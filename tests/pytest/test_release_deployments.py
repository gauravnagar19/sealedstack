"""D-060: released versions are recorded as GitHub Deployments in the app repo."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import botpermissions, deployapps, forge, onboard  # noqa: E402
from sealedstack.http import Route, StubClient  # noqa: E402

APP_YML = REPO / ".github" / "workflows" / "app.yml"


def load_app() -> dict:
    doc = yaml.safe_load(APP_YML.read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def release_steps() -> list[dict]:
    return load_app()["jobs"]["release"]["steps"]


def step_named(name: str) -> dict:
    return next(s for s in release_steps() if s.get("name") == name)


# --------------------------------------------------------------------------- workflow shape
def test_release_creates_the_deployment_with_the_built_in_token_before_deploying():
    steps = release_steps()
    names = [s.get("name") for s in steps]
    start = step_named("Start the production deployment record")
    assert start["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    run = start["run"]
    assert 'POST "/repos/${GITHUB_REPOSITORY}/deployments"' in run
    for needle in ('"environment": "production"', '"ref": version', '"version": version', '"servers": sorted(ids)'):
        assert needle in run
    assert 'state=in_progress' in run
    assert names.index("Start the production deployment record") < names.index("Update release-owned fields and deploy")


def test_release_marks_success_after_the_deploy_and_failure_when_it_fails():
    names = [s.get("name") for s in release_steps()]
    ok = step_named("Mark the deployment successful")
    bad = step_named("Mark the deployment failed")
    assert names.index("Update release-owned fields and deploy") < names.index("Mark the deployment successful")
    assert "success()" in ok["if"] and "state=success" in ok["run"]
    assert "failure()" in bad["if"] and "state=failure" in bad["run"]
    for step in (ok, bad):
        assert step["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
        assert "steps.deployment.outputs.id" in step["env"]["DEPLOYMENT_ID"]


def test_app_workflow_has_no_repository_variable_write_and_no_bot_token():
    text = APP_YML.read_text(encoding="utf-8")
    assert "gh variable set" not in text
    assert "/actions/variables" not in text
    assert "SEALEDSTACK_BOT_TOKEN" not in text
    # nothing in the release job uses a token other than the built-in one
    for step in release_steps():
        token = str((step.get("env") or {}).get("GH_TOKEN", ""))
        assert token in ("", "${{ secrets.GITHUB_TOKEN }}")


def test_app_workflow_requests_deployments_write_and_the_callers_grant_it():
    doc = load_app()
    assert doc["permissions"]["deployments"] == "write"
    template = (REPO / "examples" / "app-template" / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    generated = onboard.render_release_workflow("example-owner", "0.5.0")
    golden = (REPO / "tests" / "golden" / "onboarding" / "files" / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for text in (template, generated, golden):
        caller = yaml.safe_load(text)["jobs"]["sealedstack"]["permissions"]
        assert caller["deployments"] == "write"
        assert caller["contents"] == "write" and caller["id-token"] == "write"
    # the header comment of the reusable workflow shows the same caller permissions
    assert "deployments: write" in APP_YML.read_text(encoding="utf-8").split("name: app-release")[0]


def test_prune_reads_the_deployment_before_the_deprecated_variable():
    prune = "\n".join(str(s.get("run", "")) for s in load_app()["jobs"]["prune"]["steps"])
    assert "deployments?environment=production" in prune
    assert prune.index("deployments?environment=production") < prune.index("${DEPLOYED_VERSION:-}")


# --------------------------------------------------------------------------- in-memory forge
def make_forge_with(*deployments):
    f = forge.InMemoryForge()
    for kwargs in deployments:
        f.add_deployment("o/shop", **kwargs)
    return f


def test_the_latest_successful_deployment_wins_over_a_newer_failed_one():
    f = make_forge_with(
        {"ref": "v0.0.3", "statuses": ["in_progress", "success"], "payload": {"version": "v0.0.3", "servers": ["server-1"]}},
        {"ref": "v0.0.4", "statuses": ["in_progress", "failure"]},
    )
    got = f.latest_successful_deployment("o/shop")
    assert got["version"] == "v0.0.3" and got["ref"] == "v0.0.3" and got["servers"] == ["server-1"]


def test_a_newer_success_wins_and_an_older_one_marked_inactive_still_counts():
    f = make_forge_with(
        {"ref": "v0.0.1", "statuses": ["in_progress", "success", "inactive"]},
        {"ref": "v0.0.2", "statuses": ["in_progress", "success"]},
    )
    assert f.latest_successful_deployment("o/shop")["version"] == "v0.0.2"
    only_old = make_forge_with({"ref": "v0.0.1", "statuses": ["in_progress", "success", "inactive"]})
    assert only_old.latest_successful_deployment("o/shop")["version"] == "v0.0.1"


def test_unfinished_and_other_environment_deployments_are_ignored():
    f = make_forge_with(
        {"ref": "v0.0.1", "statuses": ["in_progress"]},
        {"ref": "v0.0.2", "statuses": ["success"], "environment": "staging"},
        {"ref": "v0.0.3", "statuses": []},
    )
    assert f.latest_successful_deployment("o/shop") == {}
    assert f.latest_successful_deployment("o/other") == {}


# --------------------------------------------------------------------------- GitHubForge
def github_with(deployments, statuses_by_id):
    http = StubClient()
    for dep_id, statuses in statuses_by_id.items():
        http.add(f"/deployments/{dep_id}/statuses", Route(json_body=statuses))
    http.add("/deployments?", Route(json_body=deployments))
    return forge.GitHubForge("github_pat_x", http=http, seal=None), http


def test_github_forge_reads_deployments_newest_first_and_checks_statuses():
    f, http = github_with(
        [
            {"id": 12, "ref": "v0.0.4", "payload": {"version": "v0.0.4"}},
            {"id": 11, "ref": "v0.0.3", "payload": json.dumps({"version": "v0.0.3", "servers": ["server-1", "server-2"]})},
        ],
        {12: [{"state": "failure"}, {"state": "in_progress"}], 11: [{"state": "inactive"}, {"state": "success"}]},
    )
    got = f.latest_successful_deployment("o/shop")
    assert got == {"id": 11, "ref": "v0.0.3", "version": "v0.0.3", "servers": ["server-1", "server-2"]}
    urls = [r["url"] for r in http.requests]
    assert urls[0] == "https://api.github.com/repos/o/shop/deployments?environment=production&per_page=30"
    assert all(r["method"] == "GET" for r in http.requests)  # read-only: Deployments: read is enough


def test_github_forge_falls_back_to_the_ref_when_the_payload_has_no_version():
    f, _ = github_with([{"id": 5, "ref": "v0.0.9", "payload": {}}], {5: [{"state": "success"}]})
    assert f.latest_successful_deployment("o/shop")["version"] == "v0.0.9"


def test_deployments_read_is_a_listed_bot_permission_and_a_check_probe():
    assert botpermissions.CALLS[("GET", "/repos/{}/deployments")] == ("Deployments", "read")
    assert botpermissions.CALLS[("GET", "/repos/{}/deployments/{}/statuses")] == ("Deployments", "read")
    deployments = next(p for p in botpermissions.PERMISSIONS if p.name == "Deployments")
    assert deployments.access == "read" and not deployments.optional
    from sealedstack import preflight

    assert any(name == "Deployments" and "app" in applies for name, _path, applies in preflight._GITHUB_READ_PROBES)


# --------------------------------------------------------------------------- infra side
def test_released_version_comes_from_the_deployment():
    f = make_forge_with({"ref": "v0.0.7", "statuses": ["success"]})
    out = io.StringIO()
    assert deployapps.released_version("shop", "o/shop", {}, forge=f, out=out) == "v0.0.7"
    assert "deprecated" not in out.getvalue().lower()


def test_the_deployment_beats_the_deprecated_variable():
    f = make_forge_with({"ref": "v0.0.7", "statuses": ["success"]})
    f.set_variable("o/shop", "SEALEDSTACK_RELEASED_VERSION", "v0.0.1")
    out = io.StringIO()
    assert deployapps.released_version("shop", "o/shop", {"SEALEDSTACK_RELEASED_VERSION": "v0.0.2"}, forge=f, out=out) == "v0.0.7"
    assert out.getvalue() == ""


def test_no_deployment_means_no_version():
    f = forge.InMemoryForge()
    assert deployapps.released_version("shop", "o/shop", {}, forge=f, out=io.StringIO()) == ""


def test_the_deprecated_repo_variable_still_works_but_warns():
    f = forge.InMemoryForge()
    f.set_variable("o/shop", "SEALEDSTACK_RELEASED_VERSION", "v0.0.5")
    out = io.StringIO()
    assert deployapps.released_version("shop", "o/shop", {}, forge=f, out=out) == "v0.0.5"
    text = out.getvalue()
    assert "WARNING" in text and "deprecated" in text and "v0.0.5" in text and "D-060" in text


def test_the_deprecated_environment_variable_still_works_but_warns():
    out = io.StringIO()
    assert deployapps.released_version("shop", "o/shop", {"SEALEDSTACK_RELEASED_VERSION_SHOP": "v9"}, out=out) == "v9"
    assert "deprecated" in out.getvalue()


def test_an_unreadable_deployments_api_names_the_missing_permission():
    class Denied(forge.InMemoryForge):
        def latest_successful_deployment(self, repo, environment="production"):
            raise forge.ForgeError("GET /repos/o/shop/deployments failed (HTTP 403)")

    out = io.StringIO()
    assert deployapps.released_version("shop", "o/shop", {}, forge=Denied(), out=out) == ""
    assert "Deployments: read" in out.getvalue() and "HTTP 403" in out.getvalue()


# --------------------------------------------------------------------------- deploy-apps end to end (stubs)
class FakeDokploy:
    def __init__(self):
        self.rows: dict[str, list] = {}
        self.envs: dict[str, str] = {}
        self.deployed: list[str] = []

    def deployments(self, compose_id):
        return list(self.rows.get(compose_id, []))

    def compose_env(self, compose_id):
        return self.envs.get(compose_id, "")

    def compose_update(self, compose_id, env):
        self.envs[compose_id] = env

    def compose_deploy(self, compose_id):
        self.deployed.append(compose_id)
        self.rows.setdefault(compose_id, []).append({"deploymentId": f"d{len(self.deployed)}", "status": "done", "createdAt": str(len(self.deployed))})


class FakeState:
    def output(self, name):
        return {"shop": {"server-1": "compose-1"}}


def run_deploy_apps(tmp_path, f, env=None):
    from sealedstack import config as config_mod

    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "stack.yaml").write_text(yaml.safe_dump({"name": "example-infra", "slots": {"secrets": "env"}}))
    (cfg / "apps.yaml").write_text("{}\n")
    (cfg / "servers.yaml").write_text("servers: {}\n")
    config = config_mod.load_config(config_dir=cfg, code_root=tmp_path, env={})
    apps = tmp_path / ".build" / "apps"
    apps.mkdir(parents=True)
    (apps / "shop.json").write_text(json.dumps({"name": "shop", "source": {"repo": "o/shop"}, "manifest": {"build": {"mode": "ci"}}}))
    client = FakeDokploy()
    out = io.StringIO()
    deployapps.run(config, env or {}, client=client, state=FakeState(), source=object(), forge=f, sleep=lambda _: None, out=out)
    return client, out.getvalue()


def test_a_fresh_compose_is_redeployed_at_the_latest_successful_deployment(tmp_path):
    f = make_forge_with(
        {"ref": "v0.0.3", "statuses": ["success"]},
        {"ref": "v0.0.4", "statuses": ["failure"]},
    )
    client, text = run_deploy_apps(tmp_path, f)
    assert client.envs["compose-1"] == "APP_VERSION=v0.0.3"
    assert client.deployed == ["compose-1"]
    assert "redeploying recorded version v0.0.3" in text


def test_no_successful_deployment_means_no_redeploy_and_says_why(tmp_path):
    f = make_forge_with({"ref": "v0.0.4", "statuses": ["in_progress", "failure"]})
    client, text = run_deploy_apps(tmp_path, f)
    assert client.deployed == [] and client.envs == {}
    assert "no successful production deployment is recorded in o/shop" in text
    assert "nothing to redeploy" in text and "release workflow" in text
