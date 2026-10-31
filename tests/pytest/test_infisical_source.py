"""Infisical as a native, read-only secret source (D-067).

The source reads one project environment and folder with a machine identity over the
REST API (no ``infisical`` CLI in the image). Requests are asserted exactly (URL, query,
header, form body), and a strict local fake server exercises the real HTTP client, so a
wrong URL or header cannot hide behind a mock (the lesson of the S3 bucket bug, DR-1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from fake_infisical import FakeInfisical, error  # noqa: E402
from sealedstack import config as cfg  # noqa: E402
from sealedstack import infisical, preflight, scaffold, secrets, setupjob  # noqa: E402
from sealedstack.http import HttpClient, Response, Route, StubClient, UrllibClient  # noqa: E402

PROJECT = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "client-id-0001"
CLIENT_SECRET = "c" * 40  # obviously fake: low entropy, so secret scanners ignore it
TOKEN = "t" * 40
SETTINGS = infisical.Settings(project_id=PROJECT)
LOGIN = "https://app.infisical.com/api/v1/auth/universal-auth/login"
IDENTITY = {"INFISICAL_CLIENT_ID": CLIENT_ID, "INFISICAL_CLIENT_SECRET": CLIENT_SECRET}


class Seq(HttpClient):
    """Scripted responses in order; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "form": form_body})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(secrets_list):
    items = [{"secretKey": k, "secretValue": v, "secretValueHidden": False} for k, v in secrets_list.items()]
    return Response(200, body=json.dumps({"secrets": items, "imports": []}).encode())


def login_ok(token="jwt-1"):
    return Response(200, body=json.dumps({"accessToken": token, "expiresIn": 7200, "tokenType": "Bearer"}).encode())


def err(status, message, headers=None):
    return Response(status, headers=headers or {}, body=json.dumps(error(status, message)).encode())


def source(http, env=None, settings=SETTINGS, **kw):
    env = {"INFISICAL_TOKEN": TOKEN} if env is None else env
    return infisical.InfisicalSource(env, settings, http=http, sleep=kw.pop("sleep", lambda s: None), **kw)


# -- settings ----------------------------------------------------------------------------
def test_settings_come_from_stack_yaml_with_sensible_defaults():
    got = infisical.load_settings({"providers": {"secrets_infisical": {"project_id": PROJECT}}})
    assert got == infisical.Settings(project_id=PROJECT, environment="prod", secret_path="/", apps_secret_path="", site_url="https://app.infisical.com")


def test_a_providers_file_wins_over_stack_yaml(tmp_path):
    (tmp_path / "providers").mkdir()
    (tmp_path / "providers" / "secrets_infisical.yaml").write_text("environment: staging\nsite_url: https://eu.infisical.com/\n")
    stack = {"providers": {"secrets_infisical": {"project_id": PROJECT, "environment": "prod"}}}
    got = infisical.load_settings(stack, tmp_path)
    assert got.environment == "staging" and got.project_id == PROJECT
    assert got.site_url == "https://eu.infisical.com"  # no trailing slash


def test_folders_are_normalised_and_the_apps_folder_falls_back_to_the_infra_one():
    assert infisical.normalise_path("") == "/" and infisical.normalise_path("apps") == "/apps"
    assert infisical.normalise_path("/apps/") == "/apps"
    s = infisical.Settings(project_id="p", secret_path="infra", apps_secret_path="")
    assert s.path_for(False) == "/infra" and s.path_for(True) == "/infra"
    s = infisical.Settings(project_id="p", secret_path="infra", apps_secret_path="apps")
    assert s.path_for(False) == "/infra" and s.path_for(True) == "/apps"


@pytest.mark.parametrize("site", ["http://infisical.example.com", "ftp://x", "infisical.example.com", ""])
def test_only_https_sites_are_accepted(site):
    problem = infisical.Settings(project_id="p", site_url=site).problem()
    assert "https://" in problem


def test_plain_http_is_allowed_only_on_loopback():
    assert infisical.Settings(project_id="p", site_url="http://127.0.0.1:9999").problem() == ""
    assert infisical.Settings(project_id="p", site_url="https://secrets.example.com").problem() == ""


def test_a_missing_project_id_is_one_clear_error_and_no_request():
    http = Seq(ok({}))
    result = source(http, settings=infisical.Settings()).get("A")
    assert result.errored and "providers.secrets_infisical.project_id" in result.detail
    assert http.requests == []


# -- naming ------------------------------------------------------------------------------
def test_the_name_as_written_is_tried_before_the_normalised_one():
    assert infisical.candidates("shop.KEY") == ["shop.KEY", "SHOP_KEY"]
    assert infisical.candidates("STATE_PASSPHRASE") == ["STATE_PASSPHRASE"]
    assert infisical.infisical_name("my-app.db url") == "MY_APP_DB_URL"
    assert infisical.infisical_name("2fa.KEY") == "_2FA_KEY"


# -- lookups (Token Auth) ----------------------------------------------------------------
def test_a_secret_is_found_by_its_exact_name():
    result = source(Seq(ok({"CLOUDFLARE_API_TOKEN": "cf"}))).get("CLOUDFLARE_API_TOKEN")
    assert result.found and result.value == "cf"


def test_an_app_secret_with_a_dot_is_found_under_the_normalised_name():
    assert source(Seq(ok({"SHOP_CLERK_SECRET_KEY": "sk"}))).get("shop.CLERK_SECRET_KEY").value == "sk"


def test_a_missing_secret_lists_the_names_tried_where_it_looked_and_never_the_credentials():
    result = source(Seq(ok({"OTHER": "x"}))).get("shop.KEY")
    assert result.missing
    assert "shop.KEY or SHOP_KEY" in result.detail and PROJECT in result.detail and "environment prod" in result.detail
    assert TOKEN not in result.detail


def test_an_empty_secret_is_reported_as_missing():
    result = source(Seq(ok({"A": ""}))).get("A")
    assert result.missing and "empty" in result.detail


def test_a_hidden_value_is_an_error_that_says_which_permission_is_missing():
    body = {"secrets": [{"secretKey": "A", "secretValue": "", "secretValueHidden": True}]}
    result = source(Seq(Response(200, body=json.dumps(body).encode()))).get("A")
    assert result.errored and "read value" in result.detail


def test_the_token_is_used_as_the_bearer_and_exactly_one_documented_call_is_made():
    http = Seq(ok({"A": "1", "B": "2"}))
    s = source(http)
    assert s.get("A").value == "1" and s.get("B").value == "2" and s.get("NOPE").missing
    assert len(http.requests) == 1
    request = http.requests[0]
    parts = urlsplit(request["url"])
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.infisical.com", "/api/v4/secrets")
    assert parse_qs(parts.query) == {
        "projectId": [PROJECT], "environment": ["prod"], "secretPath": ["/"], "recursive": ["false"],
        "expandSecretReferences": ["true"], "viewSecretValue": ["true"], "includeImports": ["false"],
    }
    assert request["method"] == "GET" and request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert request["form"] is None


def test_an_exported_variable_wins_without_any_request():
    http = Seq(ok({}))
    result = source(http, env={"INFISICAL_TOKEN": TOKEN, "CLOUDFLARE_API_TOKEN": "from-env"}).get("CLOUDFLARE_API_TOKEN")
    assert result.found and result.value == "from-env" and http.requests == []


def test_the_apps_source_reads_the_apps_folder():
    settings = infisical.Settings(project_id=PROJECT, secret_path="/infra", apps_secret_path="/apps")
    infra, apps = Seq(ok({})), Seq(ok({}))
    source(infra, settings=settings).get("A")
    source(apps, settings=settings, apps=True).get("A")
    assert parse_qs(urlsplit(infra.requests[0]["url"]).query)["secretPath"] == ["/infra"]
    assert parse_qs(urlsplit(apps.requests[0]["url"]).query)["secretPath"] == ["/apps"]


def test_a_custom_site_url_is_used_for_eu_and_self_hosted():
    http = Seq(ok({}))
    source(http, settings=infisical.Settings(project_id=PROJECT, site_url="https://eu.infisical.com")).get("A")
    assert http.requests[0]["url"].startswith("https://eu.infisical.com/api/v4/secrets?")


# -- lookups (Universal Auth) ------------------------------------------------------------
def test_universal_auth_logs_in_with_a_form_body_then_uses_the_access_token():
    http = Seq(login_ok("jwt-1"), ok({"A": "1"}))
    s = source(http, env=IDENTITY)
    assert s.get("A").value == "1"
    login, read = http.requests
    assert login["method"] == "POST" and login["url"] == LOGIN
    assert login["form"] == {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}
    assert read["headers"]["Authorization"] == "Bearer jwt-1"


def test_a_run_logs_in_once_however_many_secrets_it_reads():
    http = Seq(login_ok(), ok({"A": "1", "B": "2"}))
    s = source(http, env=IDENTITY)
    s.get("A"), s.get("B"), s.get("C")
    assert [r["method"] for r in http.requests] == ["POST", "GET"]


def test_a_static_token_wins_over_the_identity_and_skips_the_login():
    http = Seq(ok({"A": "1"}))
    source(http, env={**IDENTITY, "INFISICAL_TOKEN": TOKEN}).get("A")
    assert [r["method"] for r in http.requests] == ["GET"]


def test_a_rejected_login_says_which_variables_to_check_and_never_echoes_them():
    http = Seq(err(401, "Invalid credentials"))
    result = source(http, env=IDENTITY).get("A")
    assert result.errored and "INFISICAL_CLIENT_ID / INFISICAL_CLIENT_SECRET" in result.detail and "HTTP 401" in result.detail
    assert "Invalid credentials" in result.detail and CLIENT_SECRET not in result.detail
    assert len(http.requests) == 1  # no secrets call after a failed login


def test_a_login_without_an_access_token_is_an_error():
    assert source(Seq(Response(200, body=b"{}")), env=IDENTITY).get("A").errored


# -- missing credentials -----------------------------------------------------------------
def test_no_credentials_is_one_clear_error_and_no_request():
    http = Seq(ok({}))
    result = source(http, env={}).get("A")
    assert result.errored and "INFISICAL_CLIENT_ID" in result.detail and "INFISICAL_CLIENT_SECRET" in result.detail
    assert http.requests == []


@pytest.mark.parametrize("have, missing", [("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"), ("INFISICAL_CLIENT_SECRET", "INFISICAL_CLIENT_ID")])
def test_half_an_identity_names_the_missing_half(have, missing):
    s = source(Seq(ok({})), env={have: "x"})
    assert s.credentials_problem().startswith(missing)


def test_an_identity_or_a_token_has_no_credentials_problem():
    assert source(Seq(ok({})), env=IDENTITY).credentials_problem() == ""
    assert source(Seq(ok({}))).credentials_problem() == ""


# -- failures ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, needle",
    [(401, "invalid or expired"), (403, "may not read secrets"), (404, "no such project, environment or folder"),
     (400, "rejected the request"), (422, "rejected the request"), (500, "HTTP 500")],
)
def test_http_failures_are_explained_with_infisicals_own_message(status, needle):
    result = source(Seq(err(status, "the server said so")), attempts=1).get("A")
    assert result.errored and needle in result.detail and "the server said so" in result.detail
    assert TOKEN not in result.detail


def test_a_401_for_a_login_token_does_not_blame_a_static_token():
    result = source(Seq(login_ok(), err(401, "nope")), env=IDENTITY).get("A")
    assert result.errored and "TTL" not in result.detail and "INFISICAL_TOKEN" not in result.detail


def test_a_rate_limit_waits_for_retry_after_then_succeeds():
    sleeps = []
    http = Seq(err(429, "slow", {"Retry-After": "2"}), ok({"A": "1"}))
    assert source(http, sleep=sleeps.append).get("A").value == "1"
    assert sleeps == [2.0] and len(http.requests) == 2


def test_a_long_retry_after_is_capped():
    sleeps = []
    http = Seq(err(429, "slow", {"Retry-After": "9999"}), ok({"A": "1"}))
    source(http, sleep=sleeps.append).get("A")
    assert sleeps == [infisical.RETRY_AFTER_CAP]


def test_a_persistent_rate_limit_gives_up_after_three_tries_with_a_clear_message():
    http = Seq(err(429, "slow"))
    result = source(http).get("A")
    assert result.errored and "rate limit" in result.detail and len(http.requests) == 3


def test_the_login_is_retried_too():
    http = Seq(err(503, "down"), login_ok(), ok({"A": "1"}))
    assert source(http, env=IDENTITY).get("A").value == "1"


def test_a_client_error_is_not_retried():
    http = Seq(err(403, "no"))
    source(http).get("A")
    assert len(http.requests) == 1


def test_a_transport_failure_is_an_error_without_the_credentials():
    result = source(Seq(OSError("connection refused")), env=IDENTITY).get("A")
    assert result.errored and "could not reach Infisical" in result.detail
    assert CLIENT_SECRET not in result.detail


def test_a_failure_is_remembered_so_a_run_makes_one_failing_call_not_one_per_secret():
    http = Seq(err(403, "no"))
    s = source(http)
    for name in ("A", "B", "C"):
        assert s.get(name).errored
    assert len(http.requests) == 1


@pytest.mark.parametrize("body", [b"not json", b"[]", b'{"secrets": "x"}', b'{"other": 1}'])
def test_an_unexpected_body_is_an_error(body):
    assert source(Seq(Response(200, body=body))).get("A").errored


# -- build_source ------------------------------------------------------------------------
def stack_for(**providers):
    return {"slots": {"secrets": "infisical"}, "providers": {"secrets_infisical": {"project_id": PROJECT, **providers}}}


def test_build_source_picks_infisical_natively(tmp_path):
    built = secrets.build_source(stack_for(environment="staging"), tmp_path, {"INFISICAL_TOKEN": TOKEN})
    assert isinstance(built, infisical.InfisicalSource) and built.apps is False
    assert built.settings.environment == "staging" and built.settings.project_id == PROJECT


def test_infisical_is_no_longer_a_command_line_backend():
    assert "infisical" not in secrets.CLI_BACKENDS
    legacy = secrets.CliSource("infisical", {"slots": {"secrets": "infisical"}}, Path("."), {}, runner=lambda argv: (0, "x", ""))
    assert legacy.get("ANY").errored  # the old `infisical secrets get` branch is gone


def test_app_secrets_use_the_apps_folder(tmp_path):
    stack = stack_for()
    assert secrets.build_source(stack, tmp_path, {}, project="prod").apps is True
    assert secrets.build_source(stack, tmp_path, {}, project="infra").apps is False
    custom = {**stack, "secrets": {"project": "core"}}
    assert secrets.build_source(custom, tmp_path, {}, project="core").apps is False
    assert secrets.build_source(custom, tmp_path, {}, project="prod").apps is True


# -- the real HTTP client against the strict fake server ---------------------------------
PROJECTS = {PROJECT: {"prod": {"/": {"STATE_PASSPHRASE": "p" * 20, "SHOP_KEY": "v"}, "/apps": {"SHOP_KEY": "app-v"}}}}


def real(fake, env=None, settings=None, **kw):
    settings = settings or infisical.Settings(project_id=PROJECT, site_url=fake.endpoint)
    return infisical.InfisicalSource({**IDENTITY, **(env or {})} if env != {} else {}, settings, http=UrllibClient(), sleep=lambda s: None, **kw)


def test_end_to_end_universal_auth_over_real_http():
    with FakeInfisical(PROJECTS, identities={CLIENT_ID: CLIENT_SECRET}) as fake:
        s = real(fake)
        assert s.get("STATE_PASSPHRASE").value == "p" * 20
        assert s.get("shop.KEY").value == "v"
        assert s.get("NOPE").missing
    login, read = fake.requests
    assert login["path"] == "/api/v1/auth/universal-auth/login"
    assert login["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert login["form"] == {"clientId": [CLIENT_ID], "clientSecret": [CLIENT_SECRET]}
    assert read["path"] == "/api/v4/secrets" and read["query"]["projectId"] == [PROJECT]
    assert read["query"]["environment"] == ["prod"] and read["query"]["secretPath"] == ["/"]
    assert read["headers"]["authorization"] == "Bearer access-1"


def test_end_to_end_token_auth_over_real_http():
    with FakeInfisical(PROJECTS, tokens={TOKEN}) as fake:
        assert real(fake, env={"INFISICAL_TOKEN": TOKEN}).get("STATE_PASSPHRASE").found
    assert [r["method"] for r in fake.requests] == ["GET"]


def test_end_to_end_the_apps_folder():
    with FakeInfisical(PROJECTS, tokens={TOKEN}) as fake:
        settings = infisical.Settings(project_id=PROJECT, apps_secret_path="apps", site_url=fake.endpoint)
        assert real(fake, env={"INFISICAL_TOKEN": TOKEN}, settings=settings, apps=True).get("SHOP_KEY").value == "app-v"


def test_end_to_end_a_wrong_client_secret_is_a_401_with_infisicals_message():
    with FakeInfisical(PROJECTS, identities={CLIENT_ID: CLIENT_SECRET}) as fake:
        result = real(fake, env={"INFISICAL_CLIENT_SECRET": "wrong"}).get("A")
    assert result.errored and "HTTP 401" in result.detail and "Invalid credentials" in result.detail


def test_end_to_end_an_unknown_folder_is_a_404():
    with FakeInfisical(PROJECTS, tokens={TOKEN}) as fake:
        settings = infisical.Settings(project_id=PROJECT, secret_path="/nope", site_url=fake.endpoint)
        result = real(fake, env={"INFISICAL_TOKEN": TOKEN}, settings=settings).get("A")
    assert result.errored and "HTTP 404" in result.detail and "/nope" in result.detail


def test_end_to_end_an_unknown_token_is_a_401():
    with FakeInfisical(PROJECTS, tokens={TOKEN}) as fake:
        result = real(fake, env={"INFISICAL_TOKEN": "z" * 40}).get("A")
    assert result.errored and "HTTP 401" in result.detail


def test_end_to_end_hidden_values_over_real_http():
    with FakeInfisical(PROJECTS, tokens={TOKEN}, hidden={"SHOP_KEY"}) as fake:
        s = real(fake, env={"INFISICAL_TOKEN": TOKEN})
        assert s.get("STATE_PASSPHRASE").found
        assert s.get("SHOP_KEY").errored


def test_end_to_end_rate_limit_then_success_reads_the_retry_after_header():
    sleeps = []
    with FakeInfisical(PROJECTS, tokens={TOKEN}) as fake:
        fake.script.append((429, {"retry-after": "2"}, error(429, "slow")))
        s = infisical.InfisicalSource({"INFISICAL_TOKEN": TOKEN}, infisical.Settings(project_id=PROJECT, site_url=fake.endpoint),
                                      http=UrllibClient(), sleep=sleeps.append)
        assert s.get("STATE_PASSPHRASE").found
    assert sleeps == [2.0] and len(fake.requests) == 2


# -- check, Setup checklist, devcontainer -------------------------------------------------
def make_ctx(tmp_path, *, env, http=None, providers=None):
    root = tmp_path / "infra"
    (root / "config").mkdir(parents=True)
    stack = {"name": "x", "domain": "example.com", "owner_email": "you@example.com",
             "slots": {"compute": "any-ssh", "mesh": "tailscale", "ingress": "cloudflare", "gate": "cloudflare_access",
                       "secrets": "infisical", "backup": "r2", "platform": "dokploy"},
             "providers": {"secrets_infisical": {"project_id": PROJECT} if providers is None else providers},
             "mesh": {"tailnet": "tail1234.ts.net", "policy": "unmanaged"}}
    (root / "config" / "stack.yaml").write_text(yaml.safe_dump(stack))
    (root / "config" / "servers.yaml").write_text("servers: {}\n")
    (root / "config" / "apps.yaml").write_text("apps: {}\n")
    config = cfg.load_config(root / "config", REPO, {})
    src = secrets.build_source(config.stack, config.config_dir, env, http=http)
    return config, preflight.build_context(config, env=env, secrets=src, version="0.6.0", build_dir=root / ".build")


def rows(results):
    return {r.id: r for r in results}


def stub(*, passphrase=True):
    body = {"secrets": [{"secretKey": "STATE_PASSPHRASE", "secretValue": "p" * 20, "secretValueHidden": False}] if passphrase else []}
    return (StubClient()
            .add("universal-auth/login", Route(200, json_body={"accessToken": "jwt-1"}))
            .add("api/v4/secrets", Route(200, json_body=body)))


def test_check_says_exactly_what_to_do_when_there_are_no_credentials(tmp_path):
    _, ctx = make_ctx(tmp_path, env={})
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "INFISICAL_CLIENT_ID" in row.detail and "INFISICAL_CLIENT_SECRET" in row.detail
    assert "machine identity" in row.fix and "repository secrets INFISICAL_CLIENT_ID and INFISICAL_CLIENT_SECRET" in row.fix


def test_check_names_the_missing_half_of_an_identity(tmp_path):
    _, ctx = make_ctx(tmp_path, env={"INFISICAL_CLIENT_ID": CLIENT_ID})
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and row.detail.startswith("INFISICAL_CLIENT_SECRET is not set")


def test_check_reports_a_reachable_source_with_project_environment_and_folder(tmp_path):
    _, ctx = make_ctx(tmp_path, env=IDENTITY, http=stub())
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "ok" and f"Infisical project {PROJECT}, environment prod, path /" in row.detail
    assert "project 'infra'" not in row.detail


def test_check_explains_a_rejected_login(tmp_path):
    http = StubClient().add("universal-auth/login", Route(401, json_body=error(401, "Invalid credentials")))
    _, ctx = make_ctx(tmp_path, env=IDENTITY, http=http)
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "HTTP 401" in row.detail and CLIENT_SECRET not in row.detail + row.fix


def test_check_says_when_the_project_id_is_not_filled_in(tmp_path):
    _, ctx = make_ctx(tmp_path, env=IDENTITY, http=stub(), providers={"project_id": ""})
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "providers.secrets_infisical.project_id" in row.detail


def test_a_missing_secret_names_project_environment_and_folder(tmp_path):
    _, ctx = make_ctx(tmp_path, env=IDENTITY, http=stub())
    row = rows(preflight.check_secrets(ctx))["secrets.cloudflare_api_token"]
    assert row.status == "fail" and f"Infisical project {PROJECT}, environment prod, path /" in row.detail + row.fix


def test_setup_accepts_infisical_and_writes_the_provider_skeleton_into_the_config():
    args = setupjob.parse_args(["--domain", "example.com", "--email", "you@example.com", "--server", "server-1", "--secrets", "infisical"])
    assert args.secrets == "infisical"
    files = setupjob.build_files(args, repo="you/my-infra", version="0.6.0", owner="you")
    stack = yaml.safe_load(files["config/stack.yaml"])
    assert stack["slots"]["secrets"] == "infisical"
    assert stack["providers"]["secrets_infisical"] == {"project_id": "", "environment": "prod", "secret_path": "/"}


def test_other_sources_get_no_infisical_provider_block():
    args = setupjob.parse_args(["--domain", "example.com", "--email", "you@example.com", "--server", "server-1"])
    stack = yaml.safe_load(setupjob.build_files(args, repo="you/my-infra", version="0.6.0", owner="you")["config/stack.yaml"])
    assert "providers" not in stack


def test_the_devcontainer_passes_through_the_infisical_credentials_only_for_infisical():
    env = json.loads(scaffold.devcontainer_json("ghcr.io/you/sealedstack:0.6.0", "infisical"))["remoteEnv"]
    assert set(env) == {"INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_TOKEN", "GH_TOKEN"}
    assert set(json.loads(scaffold.devcontainer_json("ghcr.io/you/sealedstack:0.6.0"))["remoteEnv"]) == {"BWS_ACCESS_TOKEN", "GH_TOKEN"}


def test_the_setup_checklist_names_infisical_not_a_project_called_infra(tmp_path):
    config, ctx = make_ctx(tmp_path, env=IDENTITY, http=stub())
    results = preflight.check_secrets(ctx)
    items = setupjob.checklist_items(results, config, repo="you/my-infra", owner="you", version="0.6.0")
    text = json.dumps([vars(i) for i in items])
    assert f"Infisical project {PROJECT}, environment prod" in text
    assert "project `infra`" not in text


# -- the workflow ------------------------------------------------------------------------
IDENTITY_VARS = ("INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_TOKEN")


def workflow():
    return yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))


def test_the_reusable_workflow_declares_the_infisical_credentials_as_optional_secrets():
    declared = workflow()[True]["workflow_call"]["secrets"]
    for name in IDENTITY_VARS:
        assert declared[name] == {"required": False}


def test_the_credentials_reach_the_engine_by_name_and_never_on_a_command_line():
    steps = workflow()["jobs"]["run"]["steps"]
    plain = next(s for s in steps if str(s.get("name", "")).startswith("sealedstack ") and "(secret source ci)" not in s["name"])
    for name in IDENTITY_VARS:
        assert plain["env"][name] == "${{ secrets." + name + " }}"
        assert f"-e {name}" in plain["run"]
        assert "secrets." not in plain["run"]  # the value is never interpolated into the script


def test_the_tailscale_fallback_can_read_the_oauth_client_from_infisical():
    steps = workflow()["jobs"]["run"]["steps"]
    trust = next(s for s in steps if s.get("id") == "ts")
    assert '[ -n "$INFISICAL_CLIENT_ID" ]' in trust["run"] and '[ -n "$INFISICAL_TOKEN" ]' in trust["run"]
    assert trust["env"]["INFISICAL_CLIENT_ID"] == "${{ secrets.INFISICAL_CLIENT_ID }}"
    fallback = next(s for s in steps if s.get("id") == "ts_secret")
    for name in IDENTITY_VARS:
        assert f"-e {name}" in fallback["run"] and fallback["env"][name] == "${{ secrets." + name + " }}"


def test_the_drift_workflow_passes_the_credentials_too():
    text = (REPO / ".github" / "workflows" / "drift.yml").read_text(encoding="utf-8")
    for name in IDENTITY_VARS:
        assert f"{name}: ${{{{ secrets.{name} }}}}" in text and f"-e {name}" in text


def test_the_ci_source_bundle_is_still_not_forwarded_for_infisical():
    plain = next(s for s in workflow()["jobs"]["run"]["steps"]
                 if str(s.get("name", "")).startswith("sealedstack ") and "(secret source ci)" not in s["name"])
    assert "SEALEDSTACK_CI_SECRETS" not in json.dumps(plain)


def test_the_registry_entry_is_native_and_needs_no_cli():
    entry = next(e for e in yaml.safe_load((REPO / "registry" / "secrets.yaml").read_text())["implementations"] if e["id"] == "infisical")
    assert entry["impl"] == {"adapter": "engine/sealedstack/infisical.py"}
    assert (REPO / entry["impl"]["adapter"]).is_file()
    assert entry["secrets"] == ["INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"]
    assert {"project_id", "environment", "secret_path", "apps_secret_path", "site_url"} <= set(entry["config"])


def test_the_engine_image_never_needed_an_infisical_cli():
    for dockerfile in ("Dockerfile", "toolbox/Dockerfile"):
        assert "infisical" not in (REPO / dockerfile).read_text(encoding="utf-8").lower()


# -- the docs cannot drift from the code ---------------------------------------------------
def prerequisites_section():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    return text.split("## Using Infisical instead of Bitwarden", 1)[1].split("## Server credentials", 1)[0]


def test_the_docs_tell_users_what_to_create_and_that_the_identity_is_read_only():
    section = prerequisites_section()
    for name in IDENTITY_VARS:
        assert name in section, name
    for needle in ("Universal Auth", "slots.secrets: infisical", "--secrets infisical", "project_id", "secret_path",
                   "apps_secret_path", "site_url", "read-only"):
        assert needle in section, needle


def test_every_setting_the_code_reads_is_documented():
    section = prerequisites_section()
    for key in ("project_id", "environment", "secret_path", "apps_secret_path", "site_url"):
        assert f"`{key}`" in section, key


def test_the_naming_table_in_the_prerequisites_matches_the_code():
    section = prerequisites_section()
    block = section.split("### Infisical secret names", 1)[1].split("**Limits to know:**", 1)[0]
    rows = [line for line in block.splitlines() if line.startswith("| `")]
    assert len(rows) >= 3
    for row in rows:
        left, right = [cell.strip() for cell in row.strip("|").split("|")]
        if right == "the same":
            keys = [k.strip().strip("`") for k in left.split(",")]
            assert all(infisical.infisical_name(k) == k for k in keys), row
        else:
            assert infisical.infisical_name(left.strip("`")) == right.strip("`"), row


def test_d067_is_recorded():
    text = (REPO / "docs" / "decisions.md").read_text(encoding="utf-8")
    assert "### D-067" in text and "Infisical is a native, read-only secret source" in text
