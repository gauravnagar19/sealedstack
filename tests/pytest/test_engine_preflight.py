"""Tests for the native preflight ``check`` (spec 7).

Every API check runs against an injected ``StubClient`` (no network) and the
config area runs against a real temp ``config/`` built from the examples.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import config as cfg  # noqa: E402
from sealedstack import preflight  # noqa: E402
from sealedstack.http import Route, StubClient  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

STACK = {
    "name": "example-infra",
    "domain": "example.com",
    "owner_email": "you@example.com",
    "admin_user": "admin",
    "slots": {
        "compute": "hetzner",
        "mesh": "tailscale",
        "ingress": "cloudflare",
        "gate": "cloudflare_access",
        "secrets": "bitwarden",
        "backup": "r2",
        "platform": "dokploy",
        "registry": "ghcr",
    },
    "cloudflare": {"account_id": "acct123", "zone": "example.com", "team_name": "example"},
    "gate": {"policies": {"owner-only": {"emails": ["you@example.com"], "session": "24h"}}, "catch_all": True},
    "edge_security": {"bot_fight_mode": True, "block_ai_bots": True, "blocked_paths": ["/.env", "*.php"]},
    "state": {"backend": "r2", "bucket": "sealedstack-state"},
    "mesh": {"policy": "unmanaged", "tailnet": "example.ts.net"},
}

SERVERS = {
    "servers": {
        "web-1": {
            "provider": "hetzner",
            "role": "control",
            "os": "ubuntu-24.04",
            "server_type": "cx22",
            "location": "fsn1",
        }
    }
}

APPS = {
    "apps": {
        "hello": {
            "source": {"image": "traefik/whoami:v1.10.3"},
            "servers": ["web-1"],
            "hostnames": ["hello.example.com"],
            "access": "owner-only",
        }
    }
}


def make_config(tmp_path, *, stack=None, servers=None, apps=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    stack = dict(stack or STACK)
    servers = servers if servers is not None else SERVERS
    apps = apps if apps is not None else APPS
    (config_dir / "stack.yaml").write_text(yaml.safe_dump(stack), encoding="utf-8")
    (config_dir / "servers.yaml").write_text(yaml.safe_dump(servers), encoding="utf-8")
    (config_dir / "apps.yaml").write_text(yaml.safe_dump(apps), encoding="utf-8")
    return cfg.Config(REPO, config_dir, stack, servers, apps)


def load_registry():
    _, registry_mod, _, _ = preflight._load_script_modules(REPO)
    return registry_mod.load()


def make_ctx(tmp_path, config, *, env=None, http=None, secrets=None, registry=None, version="0.5.0", tcp_probe=None):
    env = dict(env or {})
    return preflight.CheckContext(
        config=config,
        env=env,
        http=http or StubClient(),
        secrets=secrets if secrets is not None else EnvSource(env),
        registry=registry if registry is not None else load_registry(),
        version=version,
        build_dir=tmp_path / "build",
        tcp_probe=tcp_probe or (lambda host, port, timeout: False),
    )


def by_id(results):
    return {result.id: result for result in results}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_check_config_valid(tmp_path):
    config = make_config(tmp_path)
    results = preflight.check_config(make_ctx(tmp_path, config))
    ids = by_id(results)
    assert ids["config.schema"].status == "ok"
    assert ids["config.cross_file"].status == "ok"
    assert ids["config.registry"].status == "ok"
    assert ids["config.render"].status == "ok"


def test_check_config_cross_file_failure(tmp_path):
    servers = {"servers": {"web-1": dict(SERVERS["servers"]["web-1"], role="worker")}}
    config = make_config(tmp_path, servers=servers)
    results = by_id(preflight.check_config(make_ctx(tmp_path, config)))
    assert results["config.cross_file"].status == "fail"
    assert "control server" in results["config.cross_file"].detail


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
NEEDED = {
    "HCLOUD_TOKEN": "x",
    "TAILSCALE_OAUTH_CLIENT_ID": "x",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "x",
    "CLOUDFLARE_API_TOKEN": "x",
    "BWS_ACCESS_TOKEN": "x",
    "R2_ACCESS_KEY_ID": "x",
    "R2_SECRET_ACCESS_KEY": "x",
    "DOKPLOY_API_KEY": "x",
    "REGISTRY_PULL_TOKEN": "x",
    "STATE_PASSPHRASE": "0123456789abcdef",
}


def test_check_secrets_all_present(tmp_path):
    config = make_config(tmp_path)
    results = preflight.check_secrets(make_ctx(tmp_path, config, env=NEEDED))
    assert all(result.status == "ok" for result in results)
    assert by_id(results)["secrets.state_passphrase"].status == "ok"


def test_check_secrets_missing_is_fail_with_fix(tmp_path):
    config = make_config(tmp_path)
    env = dict(NEEDED)
    del env["HCLOUD_TOKEN"]
    results = by_id(preflight.check_secrets(make_ctx(tmp_path, config, env=env)))
    result = results["secrets.hcloud_token"]
    assert result.status == "fail"
    assert "HCLOUD_TOKEN" in result.fix


def test_check_secrets_short_passphrase_is_fail(tmp_path):
    config = make_config(tmp_path)
    env = dict(NEEDED, STATE_PASSPHRASE="short")
    results = by_id(preflight.check_secrets(make_ctx(tmp_path, config, env=env)))
    assert results["secrets.state_passphrase"].status == "fail"
    assert "16" in results["secrets.state_passphrase"].detail


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
def test_check_versions_ok(tmp_path):
    config = make_config(tmp_path)
    results = by_id(preflight.check_versions(make_ctx(tmp_path, config, version="0.5.0")))
    assert results["versions.engine"].status == "ok"


def test_check_versions_deprecated_warns(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight, "DEPRECATED_BELOW", "0.6.0")
    config = make_config(tmp_path)
    results = by_id(preflight.check_versions(make_ctx(tmp_path, config, version="0.5.0")))
    assert results["versions.engine"].status == "warn"


def test_check_versions_affected_fails(tmp_path, monkeypatch):
    monkeypatch.setitem(preflight.AFFECTED_VERSIONS, "0.5.0", "SEC-0001")
    config = make_config(tmp_path)
    results = by_id(preflight.check_versions(make_ctx(tmp_path, config, version="0.5.0")))
    assert results["versions.engine"].status == "fail"


# --------------------------------------------------------------------------- #
# cloudflare
# --------------------------------------------------------------------------- #
def test_check_cloudflare_skips_without_token(tmp_path):
    config = make_config(tmp_path)
    results = preflight.check_cloudflare(make_ctx(tmp_path, config, env={}))
    assert results[0].status == "skip"


def test_check_cloudflare_permissions_via_mock(tmp_path):
    config = make_config(tmp_path)
    http = (
        StubClient()
        .add("user/tokens/verify", Route(json_body={"success": True, "result": {"id": "token123"}}))
        .add("zones?name=example.com", Route(json_body={"success": True, "result": [{"id": "zone123"}]}))
        .add("/dns_records", Route(json_body={"success": True}))
        .add("/cfd_tunnel", Route(json_body={"success": True}))
        .add("/access/apps", Route(json_body={"success": True}))
        .add("/r2/buckets", Route(status=403, json_body={"success": False}))
        .add("rulesets?per_page=1", Route(json_body={"success": True}))
        .add("/bot_management", Route(json_body={"success": True}))
        .add("http_request_dynamic_redirect", Route(json_body={"success": True}))
        .add("http_response_headers_transform", Route(status=404, json_body={"success": False}))  # no ruleset yet (D-078)
        .add("settings/min_tls_version", Route(status=403, json_body={"success": False}))  # D-079
        .add("access/organizations", Route(json_body={"success": True}))
    )
    ctx = make_ctx(tmp_path, config, env={"CLOUDFLARE_API_TOKEN": "abc"}, http=http)
    results = by_id(preflight.check_cloudflare(ctx))
    assert results["cloudflare.perm.headers_edit"].status == "ok"  # a 404 on an empty phase is not a missing permission
    assert results["cloudflare.perm.zone_settings_edit"].status == "fail"
    assert "Zone Settings: Edit" in results["cloudflare.perm.zone_settings_edit"].fix
    assert results["cloudflare.token"].status == "ok"
    assert results["cloudflare.zone"].status == "ok"
    assert results["cloudflare.perm.dns_edit"].status == "ok"
    assert results["cloudflare.perm.r2_edit"].status == "fail"
    assert results["cloudflare.zero_trust"].status == "ok"
    # D-054: without the R2 permission the keys can NOT be derived; check says so.
    assert results["cloudflare.r2_derived"].status == "fail"
    assert "Workers R2 Storage: Edit" in results["cloudflare.r2_derived"].fix


# --------------------------------------------------------------------------- #
# tailscale
# --------------------------------------------------------------------------- #
def test_check_tailscale_oauth_via_mock(tmp_path):
    config = make_config(tmp_path)
    http = (
        StubClient()
        .add("oauth/token", Route(json_body={"access_token": "at"}))
        .add("/devices", Route(json_body={"devices": []}))
    )
    ctx = make_ctx(
        tmp_path,
        config,
        env={"TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"},
        http=http,
    )
    results = by_id(preflight.check_tailscale(ctx))
    assert results["tailscale.tailnet"].status == "ok"
    assert results["tailscale.oauth"].status == "ok"
    assert results["tailscale.whoami"].status == "ok"


def test_check_tailscale_detects_allow_all(tmp_path):
    config = make_config(tmp_path)
    config.stack["mesh"]["policy"] = "managed"
    (config.config_dir / "tailscale-policy.hujson").write_text(
        json.dumps({"acls": [{"action": "accept", "src": ["*"], "dst": ["*"]}]}), encoding="utf-8"
    )
    results = by_id(preflight.check_tailscale(make_ctx(tmp_path, config, env={})))
    assert results["tailscale.allow_all"].status == "fail"


def test_check_tailscale_oauth_skips_without_credentials(tmp_path):
    config = make_config(tmp_path)
    results = by_id(preflight.check_tailscale(make_ctx(tmp_path, config, env={})))
    assert results["tailscale.oauth"].status == "skip"


# --------------------------------------------------------------------------- #
# forge
# --------------------------------------------------------------------------- #
FORGE_APPS = {
    "apps": {
        "app1": {
            "source": {"repo": "owner/app", "ref": "main"},
            "servers": ["web-1"],
            "hostnames": ["app.example.com"],
            "access": "owner-only",
        }
    }
}


FINE_GRAINED = "github_pat_example"


RELEASE_VARS = {"SEALEDSTACK_TS_RELEASE_CLIENT_ID": "release-client", "SEALEDSTACK_TS_RELEASE_AUDIENCE": "release-audience"}


def github_stub(*, denied=(), secrets_status=200, key_secret_status=200, repo_status=200, role=None, release_vars=None, release_status=None) -> StubClient:
    """A GitHub API stub: every read answers 200 except the permissions in ``denied``.

    ``release_vars`` is the app repo's published ``tag:release`` variables (default: both set);
    ``release_status`` forces one status (e.g. 403) for reading them.
    """
    http = StubClient()
    http.add("api.github.com/user", Route(json_body={"login": "sealedstack-bot"}))
    published = RELEASE_VARS if release_vars is None else release_vars
    for name in RELEASE_VARS:
        if release_status:
            http.add(f"/actions/variables/{name}", Route(status=release_status, json_body={}))
        elif name in published:
            http.add(f"/actions/variables/{name}", Route(json_body={"name": name, "value": published[name]}))
        else:
            http.add(f"/actions/variables/{name}", Route(status=404, json_body={}))
    probes = {
        "Contents": "/contents/",
        "Pull requests": "/pulls?state=open",
        "Issues": "/issues?per_page=1",
        "Variables": "/actions/variables",
        "Deployments": "/deployments?environment=production",
    }
    for permission, needle in probes.items():
        http.add(needle, Route(status=403 if permission in denied else 200, json_body=[]))
    http.add("/actions/secrets/SEALEDSTACK_DOKPLOY_API_KEY", Route(status=key_secret_status, json_body={"name": "SEALEDSTACK_DOKPLOY_API_KEY"}))
    http.add("/actions/secrets/public-key", Route(status=secrets_status, json_body={}))
    http.add("api.github.com/repos/", Route(status=repo_status, json_body={"permissions": role} if role else {}))
    return http


def test_check_forge_github_via_mock(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(denied=("Variables",)))
    results = by_id(preflight.check_forge(ctx))
    assert results["forge.identity"].status == "ok"
    repo = results["forge.repo.owner/app"]
    assert repo.status == "fail"
    assert "Variables" in repo.detail and "Variables: read and write" in repo.fix


def test_check_forge_names_deployments_read_when_the_token_lacks_it(tmp_path):
    # D-060: the infra side reads each app repo's latest successful production deployment
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(denied=("Deployments",)))
    repo = by_id(preflight.check_forge(ctx))["forge.repo.owner/app"]
    assert repo.status == "fail"
    assert "Deployments" in repo.detail and "Deployments: read" in repo.fix and "read and write" not in repo.fix.split("full list")[0]


def test_check_forge_probes_the_infra_repo_and_each_app_repo_with_reads_only(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub()
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED, "GITHUB_REPOSITORY": "you/my-infra"}, http=http)
    results = by_id(preflight.check_forge(ctx))
    assert results["forge.repo.you/my-infra"].status == "ok"
    assert results["forge.repo.owner/app"].status == "ok"
    assert "Secrets" in results["forge.repo.owner/app"].detail  # app repos also need the repository secret
    assert "Secrets" not in results["forge.repo.you/my-infra"].detail
    # nothing was written: harmless reads only
    assert {r["method"] for r in http.requests} == {"GET"}
    # the infra repo is not asked for variables (it never needs them)
    assert not any("you/my-infra/actions/variables" in r["url"] for r in http.requests)
    assert not any("you/my-infra/deployments" in r["url"] for r in http.requests)
    assert any("owner/app/deployments?environment=production" in r["url"] for r in http.requests)


def test_check_forge_names_the_permission_a_repo_is_missing(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(denied=("Issues", "Pull requests")))
    repo = by_id(preflight.check_forge(ctx))["forge.repo.owner/app"]
    assert repo.status == "fail" and "Issues" in repo.detail and "Pull requests" in repo.detail
    assert "Issues: read and write" in repo.fix and "Pull requests: read and write" in repo.fix


def test_check_forge_a_repo_outside_the_tokens_access_is_one_clear_failure(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(repo_status=404))
    repo = by_id(preflight.check_forge(ctx))["forge.repo.owner/app"]
    assert repo.status == "fail" and "cannot see owner/app" in repo.detail and "Repository access" in repo.fix


def test_check_forge_flags_a_user_without_push_access(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(role={"push": False, "pull": True}))
    repo = by_id(preflight.check_forge(ctx))["forge.repo.owner/app"]
    assert repo.status == "fail" and "no push access" in repo.detail


def test_check_forge_fails_when_the_token_cannot_reach_repository_secrets(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    denied = by_id(preflight.check_forge(make_ctx(
        tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub(secrets_status=403))))["forge.repo.owner/app"]
    assert denied.status == "fail" and "Secrets" in denied.detail and "Secrets: read and write" in denied.fix


def test_check_reports_the_dokploy_key_mode_per_app_repo(tmp_path):
    """FR-2: the key is a repository secret in every mode; `check` says so per app repo."""
    config = make_config(tmp_path, apps=FORGE_APPS)
    env = {"GH_TOKEN": FINE_GRAINED}
    ok = by_id(preflight.check_forge(make_ctx(tmp_path, config, env=env, http=github_stub())))["forge.dokploy_key.owner/app"]
    assert ok.status == "ok" and "repository secret" in ok.detail and "SEALEDSTACK_DOKPLOY_API_KEY" in ok.detail
    missing = by_id(preflight.check_forge(make_ctx(tmp_path, config, env=env, http=github_stub(key_secret_status=404))))["forge.dokploy_key.owner/app"]
    assert missing.status == "warn" and "run apply" in missing.fix and "GitHub Free" in missing.fix
    unreadable = by_id(preflight.check_forge(make_ctx(tmp_path, config, env=env, http=github_stub(key_secret_status=403))))["forge.dokploy_key.owner/app"]
    assert unreadable.status == "warn" and "Secrets: read" in unreadable.fix


def test_check_forge_warns_about_a_classic_token(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": "ghp_example"}, http=github_stub())
    kind = by_id(preflight.check_forge(ctx))["forge.token_type"]
    assert kind.status == "warn" and "fine-grained" in kind.fix
    fine = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=github_stub())))
    assert "forge.token_type" not in fine


# --------------------------------------------------------------------------- #
# servers
# --------------------------------------------------------------------------- #
def test_check_servers_reachable(tmp_path):
    config = make_config(tmp_path)
    ctx = make_ctx(tmp_path, config, tcp_probe=lambda host, port, timeout: host == "web-1.example.ts.net")
    results = by_id(preflight.check_servers(ctx))
    assert results["servers.web-1"].status == "ok"


def test_check_servers_unreachable(tmp_path):
    config = make_config(tmp_path)
    results = by_id(preflight.check_servers(make_ctx(tmp_path, config)))
    # web-1's provider creates the server, so an unreachable box is "not
    # bootstrapped yet" (skip), not a hard failure (D-049).
    assert results["servers.web-1"].status == "skip"


# --------------------------------------------------------------------------- #
# payload shape
# --------------------------------------------------------------------------- #
def test_json_payload_shape():
    results = [
        preflight.Result("a", "config", "ok", detail="ok"),
        preflight.Result("b", "secrets", "fail", detail="bad", fix="do x"),
    ]
    payload = preflight.check_payload(results, version="0.5.0")
    assert set(payload) == {"tool", "command", "engine_version", "ok", "counts", "results"}
    assert payload["command"] == "check"
    assert payload["ok"] is False
    assert payload["counts"] == {"ok": 1, "warn": 0, "info": 0, "fail": 1, "skip": 0}
    assert payload["results"][1]["fix"] == "do x"


def test_run_checks_never_raises_on_bad_area(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def boom(_ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(preflight, "AREA_CHECKS", (boom,))
    results = preflight.run_checks(make_ctx(tmp_path, config))
    assert results[0].status == "fail"
    assert "kaboom" in results[0].detail


# --------------------------------------------------------------------------- #
# the tag:release identity (D-057)
# --------------------------------------------------------------------------- #
RELEASE_STACK = {**STACK, "providers": {"mesh_tailscale": {"release_oauth_client_id": "rel-client", "release_audience": "rel-audience"}}}


def test_infra_release_identity_is_required_when_an_app_has_a_repo(tmp_path):
    row = by_id(preflight.check_tailscale(make_ctx(tmp_path, make_config(tmp_path, apps=FORGE_APPS), env={})))["tailscale.release_identity"]
    assert row.status == "fail"
    assert "release_oauth_client_id and release_audience" in row.detail and "app1" in row.detail
    assert "runbook T2" in row.fix and "tag:release" in row.fix and "providers.mesh_tailscale" in row.fix


def test_infra_release_identity_names_only_the_missing_key(tmp_path):
    stack = {**STACK, "providers": {"mesh_tailscale": {"release_oauth_client_id": "rel-client"}}}
    row = by_id(preflight.check_tailscale(make_ctx(tmp_path, make_config(tmp_path, stack=stack, apps=FORGE_APPS), env={})))["tailscale.release_identity"]
    assert row.status == "fail" and "release_audience" in row.detail and "release_oauth_client_id not set" not in row.detail


def test_infra_release_identity_ok_when_both_are_set_in_config_or_env(tmp_path):
    config = make_config(tmp_path / "a", stack=RELEASE_STACK, apps=FORGE_APPS)
    assert by_id(preflight.check_tailscale(make_ctx(tmp_path / "a", config, env={})))["tailscale.release_identity"].status == "ok"
    env = {"TS_RELEASE_OAUTH_CLIENT_ID": "c", "TS_RELEASE_OAUTH_AUDIENCE": "a"}  # what publish-app-settings also reads
    config = make_config(tmp_path / "b", apps=FORGE_APPS)
    assert by_id(preflight.check_tailscale(make_ctx(tmp_path / "b", config, env=env)))["tailscale.release_identity"].status == "ok"


def test_infra_release_identity_is_not_demanded_before_any_app_has_a_repo(tmp_path):
    row = by_id(preflight.check_tailscale(make_ctx(tmp_path, make_config(tmp_path), env={})))["tailscale.release_identity"]
    assert row.status == "skip" and "before the first app release" in row.fix  # image-only apps have no release workflow


def test_no_release_identity_row_when_the_mesh_is_not_tailscale(tmp_path):
    stack = {**STACK, "slots": {**STACK["slots"], "mesh": "headscale"}}
    results = by_id(preflight.check_tailscale(make_ctx(tmp_path, make_config(tmp_path, stack=stack, apps=FORGE_APPS), env={})))
    assert "tailscale.release_identity" not in results


def test_app_repo_release_variables_published_is_ok(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub()
    results = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=http)))
    row = results["forge.release_vars.owner/app"]
    assert row.status == "ok" and "SEALEDSTACK_TS_RELEASE_CLIENT_ID" in row.detail
    assert {r["method"] for r in http.requests} == {"GET"}


def test_app_repo_missing_release_variables_fail_with_the_remedy(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub(release_vars={})
    row = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=http)))["forge.release_vars.owner/app"]
    assert row.status == "fail"
    assert "owner/app" in row.detail and "SEALEDSTACK_TS_RELEASE_CLIENT_ID" in row.detail and "SEALEDSTACK_TS_RELEASE_AUDIENCE" in row.detail
    assert "run apply" in row.fix and "tag:release" in row.fix and "runbook T2" in row.fix


def test_app_repo_with_one_release_variable_missing_names_that_one(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub(release_vars={"SEALEDSTACK_TS_RELEASE_CLIENT_ID": "x"})
    row = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=http)))["forge.release_vars.owner/app"]
    assert row.status == "fail" and "SEALEDSTACK_TS_RELEASE_AUDIENCE" in row.detail and "RELEASE_CLIENT_ID" not in row.detail


def test_app_repo_with_an_empty_release_variable_counts_as_missing(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub(release_vars={"SEALEDSTACK_TS_RELEASE_CLIENT_ID": "", "SEALEDSTACK_TS_RELEASE_AUDIENCE": "a"})
    row = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=http)))["forge.release_vars.owner/app"]
    assert row.status == "fail" and "SEALEDSTACK_TS_RELEASE_CLIENT_ID" in row.detail


def test_unreadable_release_variables_warn_instead_of_failing(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = github_stub(release_status=403)
    row = by_id(preflight.check_forge(make_ctx(tmp_path, config, env={"GH_TOKEN": FINE_GRAINED}, http=http)))["forge.release_vars.owner/app"]
    assert row.status == "warn" and "Variables: read" in row.fix


def test_release_variables_are_checked_once_per_repo_and_only_for_tailscale(tmp_path):
    apps = {"apps": {**FORGE_APPS["apps"], "app2": {**FORGE_APPS["apps"]["app1"], "hostnames": ["two.example.com"]}}}
    http = github_stub()
    results = preflight.check_forge(make_ctx(tmp_path / "a", make_config(tmp_path / "a", apps=apps), env={"GH_TOKEN": FINE_GRAINED}, http=http))
    assert [r.id for r in results if r.id.startswith("forge.release_vars.")] == ["forge.release_vars.owner/app"]
    stack = {**STACK, "slots": {**STACK["slots"], "mesh": "headscale"}}
    other = by_id(preflight.check_forge(make_ctx(tmp_path / "b", make_config(tmp_path / "b", stack=stack, apps=FORGE_APPS), env={"GH_TOKEN": FINE_GRAINED}, http=github_stub())))
    assert not any(k.startswith("forge.release_vars.") for k in other)
    assert not any(k.startswith("forge.release_vars.") for k in by_id(preflight.check_forge(make_ctx(tmp_path / "c", make_config(tmp_path / "c", apps=FORGE_APPS), env={}))))
