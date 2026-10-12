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
    "state_passphrase": "0123456789abcdef",
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
    result = results["secrets.HCLOUD_TOKEN"]
    assert result.status == "fail"
    assert "HCLOUD_TOKEN" in result.fix


def test_check_secrets_short_passphrase_is_fail(tmp_path):
    config = make_config(tmp_path)
    env = dict(NEEDED, state_passphrase="short")
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
        .add("access/organizations", Route(json_body={"success": True}))
    )
    ctx = make_ctx(tmp_path, config, env={"CLOUDFLARE_API_TOKEN": "abc"}, http=http)
    results = by_id(preflight.check_cloudflare(ctx))
    assert results["cloudflare.token"].status == "ok"
    assert results["cloudflare.zone"].status == "ok"
    assert results["cloudflare.perm.dns_edit"].status == "ok"
    assert results["cloudflare.perm.r2_edit"].status == "fail"
    assert results["cloudflare.zero_trust"].status == "ok"
    assert results["cloudflare.r2_derived"].status == "ok"


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


def test_check_forge_github_via_mock(tmp_path):
    config = make_config(tmp_path, apps=FORGE_APPS)
    http = (
        StubClient()
        .add("api.github.com/user", Route(json_body={"login": "sealedstack-bot"}))
        .add("/pulls", Route(json_body=[]))
        .add("actions/variables", Route(status=403, json_body={}))
    )
    ctx = make_ctx(tmp_path, config, env={"GH_TOKEN": "t"}, http=http)
    results = by_id(preflight.check_forge(ctx))
    assert results["forge.identity"].status == "ok"
    assert results["forge.repo.owner/app"].status == "fail"
    assert "variables" in results["forge.repo.owner/app"].detail


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
    assert payload["counts"] == {"ok": 1, "warn": 0, "fail": 1, "skip": 0}
    assert payload["results"][1]["fix"] == "do x"


def test_run_checks_never_raises_on_bad_area(tmp_path, monkeypatch):
    config = make_config(tmp_path)

    def boom(_ctx):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(preflight, "AREA_CHECKS", (boom,))
    results = preflight.run_checks(make_ctx(tmp_path, config))
    assert results[0].status == "fail"
    assert "kaboom" in results[0].detail
