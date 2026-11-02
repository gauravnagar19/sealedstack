"""app-secrets: each app's own Dokploy secrets provider, created or attached during apply (D-088)."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from sealedstack import appsecrets, orchestrate, preflight
from sealedstack.http import Route, StubClient
from sealedstack.secrets import SecretResult


class FakeDokploy:
    def __init__(self, providers=()):
        self.providers = list(providers)
        self.posts = []

    def vault_providers(self):
        return self.providers

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {}


class FakeState:
    def __init__(self, projects):
        self.projects = projects

    def output(self, name):
        assert name == "projects"
        return self.projects


class FakeDoppler:
    def __init__(self, projects=(), names=()):
        self.projects, self.names = set(projects), list(names)
        self.created, self.minted = [], []

    def project_exists(self, project):
        return project in self.projects

    def create_project(self, project):
        self.created.append(project)
        self.projects.add(project)

    def mint_read_token(self, project, config, name):
        self.minted.append((project, config, name))
        return "dp.st.minted"

    def secret_names(self, project, config):
        return self.names


def setup(tmp_path, vault=None, name="paisa"):
    apps = tmp_path / ".build" / "apps"
    apps.mkdir(parents=True, exist_ok=True)
    data = {"name": name}
    if vault is not None:
        data["vault"] = vault
    (apps / f"{name}.json").write_text(json.dumps(data))
    return SimpleNamespace(code_root=tmp_path, name="demo", build_dir=tmp_path / ".build",
                           secret_key=lambda logical: logical, legacy_secret_key=lambda logical: "")


def run(config, client, doppler=None, projects=None):
    out = io.StringIO()
    appsecrets.run(
        config, {}, client=client, state=FakeState({"paisa": "proj-1"} if projects is None else projects),
        source=Secrets({}), doppler=doppler, out=out,
    )
    return out.getvalue()


VAULT = {"provider": "paisa", "keys": ["CLERK_SECRET_KEY", "DATABASE_URL"]}


def test_an_existing_provider_is_assigned_with_the_masked_config_sent_back(tmp_path):
    provider = {"vaultProviderId": "v1", "name": "paisa", "providerType": "doppler",
                "config": {"providerType": "doppler", "serviceToken": "********"}, "assignments": []}
    client = FakeDokploy([provider])
    text = run(setup(tmp_path, VAULT), client)
    assert client.posts == [("/api/vaultProvider.update", {
        "vaultProviderId": "v1", "name": "paisa",
        "config": {"providerType": "doppler", "serviceToken": "********"},
        "assignments": [{"projectId": "proj-1", "environmentIds": []}],
    })]
    assert "assigned to project paisa" in text


def test_an_already_assigned_provider_is_left_alone(tmp_path):
    provider = {"vaultProviderId": "v1", "name": "paisa", "config": {}, "assignments": [{"projectId": "proj-1"}]}
    client = FakeDokploy([provider])
    assert "already assigned" in run(setup(tmp_path, VAULT), client)
    assert client.posts == []


def test_without_an_admin_token_a_missing_provider_prints_the_manual_steps(tmp_path):
    client = FakeDokploy()
    text = run(setup(tmp_path, VAULT), client)
    assert client.posts == []
    assert "DOPPLER_ADMIN_TOKEN" in text and "create a Doppler provider named 'paisa'" in text


def test_with_an_admin_token_the_project_token_and_provider_are_created(tmp_path):
    client, doppler = FakeDokploy(), FakeDoppler(names=["CLERK_SECRET_KEY"])
    text = run(setup(tmp_path, VAULT), client, doppler)
    assert doppler.created == ["paisa"]
    assert doppler.minted == [("paisa", "prd", "sealedstack-dokploy-demo")]
    assert client.posts == [("/api/vaultProvider.create", {
        "name": "paisa",
        "config": {"providerType": "doppler", "serviceToken": "dp.st.minted"},
        "assignments": [{"projectId": "proj-1", "environmentIds": []}],
    })]
    assert "dp.st.minted" not in text
    assert "lacks" not in text and "DATABASE_URL" in text.split("add these keys")[1]


def test_an_existing_doppler_project_is_reused(tmp_path):
    doppler = FakeDoppler(projects=["paisa"], names=VAULT["keys"])
    text = run(setup(tmp_path, VAULT), FakeDokploy(), doppler)
    assert doppler.created == [] and len(doppler.minted) == 1
    assert "add these keys" not in text


def test_apps_without_the_object_form_or_a_project_are_skipped(tmp_path):
    config = setup(tmp_path, None)
    client = FakeDokploy()
    assert "nothing to do" in run(config, client)
    config = setup(tmp_path, VAULT)
    assert "no Dokploy project yet" in run(config, client, FakeDoppler(), projects={})
    assert client.posts == []


def test_a_service_token_is_refused_as_admin_token():
    with pytest.raises(appsecrets.AppSecretsError, match="cannot create projects"):
        appsecrets.DopplerAdmin("dp.st.abc")


def test_the_admin_client_speaks_dopplers_api_and_keeps_the_token_in_the_header():
    http = StubClient()
    http.add("/v3/projects/project?project=paisa", Route(status=404, json_body={"messages": ["not found"]}))
    http.add("/v3/projects", Route(json_body={"project": {"name": "paisa"}}))
    http.add("/v3/configs/config/tokens", Route(json_body={"token": {"key": "dp.st.new"}}))
    http.add("/v3/configs/config/secrets/names", Route(json_body={"names": ["A"]}))
    admin = appsecrets.DopplerAdmin("dp.pt.secret", http=http)
    assert admin.project_exists("paisa") is False
    admin.create_project("paisa")
    assert admin.mint_read_token("paisa", "prd", "t") == "dp.st.new"
    assert admin.secret_names("paisa", "prd") == ["A"]
    mint = http.requests[2]
    assert mint["json_body"] == {"project": "paisa", "config": "prd", "name": "t", "access": "read"}
    assert all(r["headers"]["Authorization"] == "Bearer dp.pt.secret" for r in http.requests)
    assert all("dp.pt.secret" not in r["url"] for r in http.requests)


def test_a_doppler_error_names_the_call_not_the_token():
    http = StubClient().add("/v3/projects", Route(status=403, json_body={"messages": ["no access"]}))
    with pytest.raises(appsecrets.AppSecretsError) as err:
        appsecrets.DopplerAdmin("dp.pt.secret", http=http).create_project("paisa")
    assert "HTTP 403" in str(err.value) and "no access" in str(err.value) and "dp.pt" not in str(err.value)


def test_the_phase_runs_after_platform_apply_and_before_env_sync(tmp_path):
    for command in ("apply", "up"):
        names = [p.name for p in orchestrate.phases(command, code_root=tmp_path)]
        assert names.index("platform-apply") < names.index("app-secrets") < names.index("post-apply")
    assert "app-secrets" not in [p.name for p in orchestrate.phases("plan", code_root=tmp_path)]


# -- check --------------------------------------------------------------------------------
class Secrets:
    def __init__(self, values):
        self.values = values

    def get(self, name):
        if name in self.values:
            return SecretResult(name, "found", self.values[name])
        return SecretResult(name, "missing")


def ctx(tmp_path, values, http=None):
    config = SimpleNamespace(secret_key=lambda logical: logical)
    return SimpleNamespace(config=config, env={}, http=http or StubClient(), secrets=Secrets(values), build_dir=tmp_path / ".build")


def test_check_says_nothing_for_apps_without_their_own_provider(tmp_path):
    setup(tmp_path, None)
    assert preflight.check_app_secrets(ctx(tmp_path, {})) == []


def test_check_without_admin_token_is_informational(tmp_path):
    setup(tmp_path, VAULT)
    [result] = preflight.check_app_secrets(ctx(tmp_path, {}))
    assert result.status == "info" and "DOPPLER_ADMIN_TOKEN" in result.fix


def test_check_names_missing_keys_in_an_existing_doppler_project(tmp_path):
    setup(tmp_path, VAULT)
    http = StubClient()
    http.add("/v3/projects/project", Route(json_body={"project": {"name": "paisa"}}))
    http.add("/secrets/names", Route(json_body={"names": ["CLERK_SECRET_KEY"]}))
    [result] = preflight.check_app_secrets(ctx(tmp_path, {"DOPPLER_ADMIN_TOKEN": "dp.pt.x"}, http))
    assert result.status == "warn" and "DATABASE_URL" in result.detail and "CLERK" not in result.detail


def test_check_says_apply_will_create_a_missing_doppler_project(tmp_path):
    setup(tmp_path, VAULT)
    http = StubClient().add("/v3/projects/project", Route(status=404, json_body={}))
    [result] = preflight.check_app_secrets(ctx(tmp_path, {"DOPPLER_ADMIN_TOKEN": "dp.pt.x"}, http))
    assert result.status == "ok" and "apply creates Doppler project 'paisa'" in result.detail


def test_check_fails_on_a_service_token_given_as_admin_token(tmp_path):
    setup(tmp_path, VAULT)
    [result] = preflight.check_app_secrets(ctx(tmp_path, {"DOPPLER_ADMIN_TOKEN": "dp.st.x"}))
    assert result.status == "fail"
