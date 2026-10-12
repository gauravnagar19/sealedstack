"""D-050: secret key names declared in config (logical name -> source key)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import config as cfg  # noqa: E402
from sealedstack import preflight  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402


def write_config(tmp_path, name="c", *, keys=None, servers=None, apps=None, admin=None):
    cdir = tmp_path / name
    cdir.mkdir(parents=True, exist_ok=True)
    stack = {
        "name": "example-infra",
        "domain": "example.com",
        "owner_email": "you@example.com",
        "admin_user": "admin",
        "slots": {
            "compute": "any-ssh",
            "mesh": "tailscale",
            "ingress": "cloudflare",
            "gate": "cloudflare_access",
            "secrets": "env",
            "backup": "r2",
            "platform": "dokploy",
        },
        "cloudflare": {"account_id": "acct", "zone": "example.com", "team_name": "example"},
        "gate": {"policies": {"owner-only": {"emails": ["you@example.com"], "session": "24h"}}, "catch_all": True},
        "edge_security": {"bot_fight_mode": True, "block_ai_bots": True, "blocked_paths": ["/.env"]},
        "state": {"backend": "r2", "bucket": "sealedstack-state"},
        "mesh": {"policy": "unmanaged", "tailnet": "example.ts.net"},
        "secrets": {"project": "infra", "keys": keys or {}},
        "admin": admin if admin is not None else {"ssh_keys": []},
    }
    (cdir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (cdir / "servers.yaml").write_text(
        yaml.safe_dump(servers or {"servers": {"web-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}})
    )
    (cdir / "apps.yaml").write_text(yaml.safe_dump(apps or {"apps": {}}))
    return cfg.load_config(cdir, REPO, {})


def test_secret_key_defaults_and_override(tmp_path):
    c = write_config(tmp_path, "a", keys={"CLOUDFLARE_API_TOKEN": "CF_TOKEN"})
    assert c.secret_key("CLOUDFLARE_API_TOKEN") == "CF_TOKEN"
    assert c.secret_key("DOKPLOY_API_KEY") == "DOKPLOY_API_KEY"  # default = logical name


def test_bootstrap_secret_defaults_and_override(tmp_path):
    c = write_config(tmp_path, "b", servers={"servers": {"box-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}})
    assert c.bootstrap_secrets("box-1") == ("SERVER_IP_BOX_1", "ROOT_PASSWORD_BOX_1")
    c2 = write_config(
        tmp_path,
        "c",
        servers={
            "servers": {
                "box-1": {
                    "provider": "any-ssh",
                    "role": "control",
                    "os": "ubuntu-24.04",
                    "bootstrap": {"ip_secret": "IP_X", "password_secret": "PW_X"},
                }
            }
        },
    )
    assert c2.bootstrap_secrets("box-1") == ("IP_X", "PW_X")


def test_app_secret_key_default_and_override(tmp_path):
    c = write_config(tmp_path, "d", apps={"apps": {"shop": {"secrets": {"CLERK_SECRET_KEY": "reused.key"}}}})
    assert c.app_secret_key("shop", "CLERK_SECRET_KEY") == "reused.key"
    assert c.app_secret_key("shop", "OTHER") == "shop.OTHER"


def test_check_secrets_honours_renamed_key(tmp_path):
    c = write_config(tmp_path, "e", keys={"CLOUDFLARE_API_TOKEN": "CF_TOKEN"})
    ctx = preflight.build_context(c, env={"CF_TOKEN": "x"}, secrets=EnvSource({"CF_TOKEN": "x"}), version="0.5.0")
    results = {r.id: r for r in preflight.check_secrets(ctx)}
    r = results["secrets.CLOUDFLARE_API_TOKEN"]
    assert r.status == "ok"
    assert "CF_TOKEN" in r.detail


def test_missing_secret_message_names_both(tmp_path):
    c = write_config(tmp_path, "f", keys={"CLOUDFLARE_API_TOKEN": "CF_TOKEN"})
    ctx = preflight.build_context(c, env={}, secrets=EnvSource({}), version="0.5.0")
    results = {r.id: r for r in preflight.check_secrets(ctx)}
    r = results["secrets.CLOUDFLARE_API_TOKEN"]
    assert r.status == "fail"
    assert "CLOUDFLARE_API_TOKEN" in r.detail and "CF_TOKEN" in r.detail
    assert "CF_TOKEN" in r.fix


def test_unbootstrapped_existing_server_needs_bootstrap_secrets(tmp_path):
    c = write_config(
        tmp_path, "g", servers={"servers": {"box-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}}
    )
    ctx = preflight.build_context(c, env={}, secrets=EnvSource({}), version="0.5.0", tcp_probe=lambda *a: False)
    r = {x.id: x for x in preflight.check_servers(ctx)}["servers.box-1"]
    assert r.status == "fail"
    assert "SERVER_IP_BOX_1" in r.detail and "ROOT_PASSWORD_BOX_1" in r.detail


def test_bootstrapped_present_secrets_ok_and_ip_never_printed(tmp_path):
    c = write_config(
        tmp_path, "h", servers={"servers": {"box-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}}
    )
    ctx = preflight.build_context(
        c,
        env={},
        secrets=EnvSource({"SERVER_IP_BOX_1": "203.0.113.9", "ROOT_PASSWORD_BOX_1": "pw"}),
        version="0.5.0",
        tcp_probe=lambda *a: False,
    )
    r = {x.id: x for x in preflight.check_servers(ctx)}["servers.box-1"]
    assert r.status == "ok"
    assert "203.0.113.9" not in r.detail  # the IP is never printed (D-049)
    assert "SERVER_IP_BOX_1" in r.detail


def test_admin_ssh_keys_come_from_config_not_env(tmp_path):
    c = write_config(tmp_path, "i", admin={"ssh_keys": ["ssh-ed25519 AAAA you@example.com"]})
    assert c.admin_ssh_keys() == ["ssh-ed25519 AAAA you@example.com"]
