"""`add-app` opens two pull requests (UX journey C), on an in-memory forge."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import appadd, forge, onboard  # noqa: E402
from sealedstack import config as config_mod  # noqa: E402
from sealedstack.http import HttpClient, Response  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "plain-app"
INFRA = "example-owner/my-infra"
APP = "example-owner/example-app"

STACK = {
    "name": "my-infra",
    "domain": "example.com",
    "owner_email": "you@example.com",
    "admin_user": "admin",
    "slots": {
        "compute": "any-ssh",
        "mesh": "tailscale",
        "ingress": "cloudflare",
        "gate": "cloudflare_access",
        "secrets": "bitwarden",
        "backup": "r2",
        "platform": "dokploy",
    },
    "cloudflare": {"account_id": "acct", "zone": "example.com", "team_name": "example"},
    "gate": {"policies": {"owner-only": {"emails": ["you@example.com"], "session": "24h"}}, "catch_all": True},
    "edge_security": {"bot_fight_mode": True, "block_ai_bots": True, "blocked_paths": ["/.env"]},
    "state": {"backend": "r2", "bucket": "sealedstack-state"},
    "mesh": {"policy": "unmanaged", "tailnet": "example.ts.net"},
}
SERVERS = {"servers": {"server-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}}
APPS_TEXT = """\
# Placement and exposure. No secrets, no IPs.
apps:
  hello:
    source: { image: traefik/whoami:v1.10.3 }
    servers: [server-1]
    hostnames: [hello.example.com]
    access: owner-only   # the smoke-test app
"""


def make_config(tmp_path, apps_text=APPS_TEXT):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump(STACK), encoding="utf-8")
    (cdir / "servers.yaml").write_text(yaml.safe_dump(SERVERS), encoding="utf-8")
    (cdir / "apps.yaml").write_text(apps_text, encoding="utf-8")
    return config_mod.load_config(cdir, REPO, {})


def app_forge(extra=None, skip=()):
    f = forge.InMemoryForge()
    for path in sorted(p for p in FIXTURE.rglob("*") if p.is_file()):
        rel = str(path.relative_to(FIXTURE))
        if rel in skip:
            continue
        f.files[(APP, "main", rel)] = path.read_text(encoding="utf-8")
    for rel, text in (extra or {}).items():
        f.files[(APP, "main", rel)] = text
    return f


def add_app(f, config, **overrides):
    kwargs = dict(
        infra_repo=INFRA,
        name="example-app",
        app_repo=APP,
        hosts=["app.example.com"],
        servers=[],
        access="owner-only",
        version="0.5.0",
        out=io.StringIO(),
    )
    kwargs.update(overrides)
    return appadd.open_prs(f, config, **kwargs)


# --------------------------------------------------------------------------- #
# the happy path: two PRs with the expected files and bodies
# --------------------------------------------------------------------------- #
def test_add_app_opens_the_app_pr_and_the_infra_pr(tmp_path):
    f = app_forge()
    result = add_app(f, make_config(tmp_path))

    by_repo = {pr["repo"]: pr for pr in f.prs}
    assert set(by_repo) == {APP, INFRA}
    app_pr, infra_pr = by_repo[APP], by_repo[INFRA]
    assert result.app_pr == app_pr["number"] and result.infra_pr == infra_pr["number"]

    # app PR: manifest, compose fixes, override, release workflow; old workflow gone
    assert app_pr["title"] == "Onboard to sealedstack" and app_pr["branch"] == "sealedstack/onboard"
    assert set(app_pr["files"]) == {
        "sealedstack.yaml",
        "docker-compose.yml",
        "docker-compose.override.yml",
        ".github/workflows/release.yml",
    }
    assert app_pr["deletes"] == [".github/workflows/deploy.yml", "scripts/deploy.sh"]
    assert "ports:" not in app_pr["files"]["docker-compose.yml"]
    assert "postgres" not in app_pr["files"]["docker-compose.yml"].replace("postgres://", "")
    assert "image: ghcr.io/example-owner/example-app-ui:${APP_VERSION}" in app_pr["files"]["docker-compose.yml"]
    assert "example-owner/sealedstack/.github/workflows/app.yml@v0.5.0" in app_pr["files"][".github/workflows/release.yml"]
    assert app_pr["body"].startswith("This PR prepares example-app for sealedstack. Review each change:")
    for fragment in ("`sealedstack.yaml` (new)", "removed `ports:`", "removed the `postgres` service", "added `image:", "release.yml"):
        assert fragment in app_pr["body"]

    # infra PR: only config/apps.yaml, with the new entry; hello and its comment kept
    assert infra_pr["title"] == "Add app example-app" and set(infra_pr["files"]) == {"config/apps.yaml"}
    new_apps = infra_pr["files"]["config/apps.yaml"]
    assert "# Placement and exposure. No secrets, no IPs." in new_apps and "# the smoke-test app" in new_apps
    parsed = yaml.safe_load(new_apps)["apps"]
    assert set(parsed) == {"hello", "example-app"}
    assert parsed["example-app"] == {
        "source": {"repo": APP, "ref": "main"},
        "servers": ["server-1"],  # defaults to the control server
        "hostnames": ["app.example.com"],
        "access": "owner-only",
    }

    # the infra PR states the order, and the app PR points back at it
    assert f"Waiting on: {APP}#{app_pr['number']} (onboarding). Merge that PR first" in infra_pr["body"]
    assert infra_pr["body"].startswith("Adds example-app on server-1 at https://app.example.com (login: owner-only).")
    assert "Postgres 16 (example), backups every 6h to R2" in infra_pr["body"]
    assert "**Merge this PR first.**" in app_pr["body"]
    comment = f.comments[(APP, app_pr["number"])][0].body
    assert f"{INFRA}#{infra_pr['number']}" in comment and "Merge this PR first" in comment


def test_the_onboarded_app_is_a_valid_sealedstack_app(tmp_path):
    """The generated files plus the new infra entry pass the real validation."""
    config = make_config(tmp_path)
    f = app_forge()
    add_app(f, config)
    app_pr = next(pr for pr in f.prs if pr["repo"] == APP)
    sys.path.insert(0, str(REPO / "scripts"))
    import lint_compose
    import validate

    manifest = yaml.safe_load(app_pr["files"]["sealedstack.yaml"])
    assert validate.schema_errors(manifest, validate.SCHEMAS / "sealedstack.schema.json") == []
    compose = yaml.safe_load(app_pr["files"]["docker-compose.yml"])
    assert lint_compose.lint(compose, manifest["needs"]) == []
    assert lint_compose.lint_build(compose, "ci", registry="ghcr.io", owner="example-owner", repo="example-app") == []


def test_already_onboarded_app_only_gets_the_infra_pr(tmp_path):
    f = app_forge(extra={"sealedstack.yaml": "name: example-app\nweb: { service: ui, port: 80 }\n"})
    result = add_app(f, make_config(tmp_path))
    assert result.app_pr is None
    assert [pr["repo"] for pr in f.prs] == [INFRA]
    assert "already has `sealedstack.yaml`" in f.prs[0]["body"]


def test_explicit_server_and_overrides_are_used(tmp_path):
    f = app_forge()
    add_app(f, make_config(tmp_path), servers=["server-1"], web_service="api", web_port=3000, health="/healthz")
    manifest = yaml.safe_load(next(pr for pr in f.prs if pr["repo"] == APP)["files"]["sealedstack.yaml"])
    assert manifest["web"] == {"service": "api", "port": 3000, "health": "/healthz"}


# --------------------------------------------------------------------------- #
# nothing is opened when the request is wrong
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"access": "friends"}, "neither 'public' nor a policy"),
        ({"hosts": ["app.other.org"]}, "not under 'example.com'"),
        ({"servers": ["nope"]}, "unknown server 'nope'"),
        ({"name": "hello"}, "already exists"),
    ],
)
def test_invalid_requests_open_no_pull_request(tmp_path, overrides, message):
    f = app_forge()
    with pytest.raises(config_mod.ConfigError, match=message):
        add_app(f, make_config(tmp_path), **overrides)
    assert f.prs == []


def test_a_repo_without_compose_opens_no_pull_request(tmp_path):
    f = app_forge(skip={"docker-compose.yml"})
    with pytest.raises(onboard.OnboardError, match="no compose file"):
        add_app(f, make_config(tmp_path))
    assert f.prs == []


# --------------------------------------------------------------------------- #
# helpers and the command line
# --------------------------------------------------------------------------- #
def test_append_app_entry_keeps_everything_else_byte_for_byte():
    entry = {"source": {"repo": APP, "ref": "main"}, "servers": ["server-1"], "hostnames": ["a.example.com"], "access": "public"}
    out = appadd.append_app_entry(APPS_TEXT, "example-app", entry)
    assert out.startswith(APPS_TEXT.rstrip("\n"))
    assert yaml.safe_load(out)["apps"]["example-app"] == entry
    # an empty `apps: {}` falls back to a full dump but stays valid
    assert yaml.safe_load(appadd.append_app_entry("apps: {}\n", "x", entry))["apps"]["x"] == entry


def test_registry_prefix_comes_from_config_or_the_app_owner(tmp_path):
    config = make_config(tmp_path)
    assert appadd.registry_prefix(config, "Example-Owner/example-app") == "ghcr.io/example-owner"
    config.stack["providers"] = {"registry_ghcr": {"owner": "Other"}}
    assert appadd.registry_prefix(config, "example-owner/example-app") == "ghcr.io/other"


def test_in_memory_forge_lists_files_in_a_directory():
    f = forge.InMemoryForge()
    f.files[(APP, "main", ".github/workflows/a.yml")] = "x"
    f.files[(APP, "main", ".github/workflows/b.yml")] = "y"
    f.files[(APP, "main", ".github/workflows/sub/c.yml")] = "z"
    f.files[(APP, "other", ".github/workflows/d.yml")] = "w"
    assert f.list_files(APP, ".github/workflows", "main") == [".github/workflows/a.yml", ".github/workflows/b.yml"]
    assert f.list_files(APP, "nowhere", "main") == []


def test_main_opens_prs_when_a_forge_is_available(tmp_path, monkeypatch, capsys):
    config = make_config(tmp_path)
    f = app_forge()
    monkeypatch.setenv("SEALEDSTACK_CONFIG", str(config.config_dir))
    rc = appadd.main(
        ["--name", "example-app", "--repo", APP, "--host", "app.example.com", "--access", "owner-only"],
        env={"SEALEDSTACK_CONFIG": str(config.config_dir), "GITHUB_REPOSITORY": INFRA, "GH_TOKEN": "t"},
        forge=f,
    )
    assert rc == 0
    assert {pr["repo"] for pr in f.prs} == {APP, INFRA}
    out = capsys.readouterr().out
    assert f"opened {APP}#" in out and f"opened {INFRA}#" in out


def test_main_reports_problems_without_a_traceback(tmp_path, capsys):
    config = make_config(tmp_path)
    f = app_forge()
    rc = appadd.main(
        ["--name", "example-app", "--repo", APP, "--host", "app.example.com", "--access", "friends"],
        env={"SEALEDSTACK_CONFIG": str(config.config_dir), "GITHUB_REPOSITORY": INFRA},
        forge=f,
    )
    assert rc == 2 and "not valid" in capsys.readouterr().err and f.prs == []


def test_main_local_mode_still_edits_apps_yaml(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    ran = []
    monkeypatch.setattr(appadd.subprocess, "run", lambda argv, **kw: ran.append(argv) or type("R", (), {"returncode": 0})())
    rc = appadd.main(
        ["example-app", "--repo", APP, "--host", "app.example.com", "--access", "owner-only", "--local"],
        env={"SEALEDSTACK_CONFIG": str(config.config_dir)},
    )
    assert rc == 0 and any(str(a[-2]).endswith("validate.py") for a in ran)  # the follow-up validation ran
    assert "example-app" in yaml.safe_load((config.config_dir / "apps.yaml").read_text())["apps"]


# --------------------------------------------------------------------------- #
# GitHub adapter: updates need the blob sha, deletes need it too
# --------------------------------------------------------------------------- #
class FakeGitHub(HttpClient):
    def __init__(self, existing):
        self.existing = dict(existing)  # path -> sha on the branch
        self.calls = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url, json_body))
        path = url.split("https://api.github.com", 1)[1]
        if method == "GET" and path.endswith("/git/ref/heads/main"):
            return Response(200, body=b'{"object": {"sha": "base"}}')
        if method == "GET" and "/contents/" in path:
            file = path.split("/contents/", 1)[1].split("?", 1)[0]
            if file in self.existing:
                return Response(200, body=('{"sha": "%s", "type": "file", "path": "%s"}' % (self.existing[file], file)).encode())
            return Response(404)
        if method == "POST" and path.endswith("/pulls"):
            return Response(201, body=b'{"number": 7}')
        return Response(201, body=b"{}")


def test_github_open_pr_updates_with_sha_and_deletes_existing_files():
    http = FakeGitHub({"docker-compose.yml": "sha-compose", ".github/workflows/deploy.yml": "sha-deploy"})
    gh = forge.GitHubForge("token", http=http, seal=None)
    number = gh.open_pr(
        APP,
        branch="sealedstack/onboard",
        title="Onboard to sealedstack",
        body="b",
        files={"docker-compose.yml": "new", "sealedstack.yaml": "fresh"},
        deletes=[".github/workflows/deploy.yml", "scripts/missing.sh"],
    )
    assert number == 7
    puts = {u.split("/contents/")[1]: b for m, u, b in http.calls if m == "PUT"}
    assert puts["docker-compose.yml"]["sha"] == "sha-compose"  # an update carries the blob sha
    assert "sha" not in puts["sealedstack.yaml"]  # a new file does not
    deletes = [(u.split("/contents/")[1], b) for m, u, b in http.calls if m == "DELETE"]
    assert deletes == [(".github/workflows/deploy.yml", {"message": "sealedstack: Onboard to sealedstack", "sha": "sha-deploy", "branch": "sealedstack/onboard"})]


def test_github_list_files_returns_only_files():
    class Listing(FakeGitHub):
        def request(self, method, url, **kw):
            if "/contents/.github/workflows" in url:
                return Response(
                    200,
                    body=b'[{"type": "file", "path": ".github/workflows/a.yml"}, {"type": "dir", "path": ".github/workflows/x"}]',
                )
            return Response(404)

    gh = forge.GitHubForge("token", http=Listing({}), seal=None)
    assert gh.list_files(APP, ".github/workflows") == [".github/workflows/a.yml"]
    assert gh.list_files(APP, "missing") == []
