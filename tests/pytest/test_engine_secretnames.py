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
    r = results["secrets.cloudflare_api_token"]
    assert r.status == "ok"
    assert "CF_TOKEN" in r.detail


def test_missing_secret_message_names_both(tmp_path):
    c = write_config(tmp_path, "f", keys={"CLOUDFLARE_API_TOKEN": "CF_TOKEN"})
    ctx = preflight.build_context(c, env={}, secrets=EnvSource({}), version="0.5.0")
    results = {r.id: r for r in preflight.check_secrets(ctx)}
    r = results["secrets.cloudflare_api_token"]
    assert r.status == "fail"
    assert "cloudflare_api_token" in r.detail and "CF_TOKEN" in r.detail
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


# --------------------------------------------------------------------------- #
# Task 3 (m7-complete): UX logical names, deprecated fallback, projects, netcup
# --------------------------------------------------------------------------- #
import pytest  # noqa: E402

from sealedstack import compute, secrets as secrets_mod, tofu  # noqa: E402


def test_default_keys_follow_the_ux_names(tmp_path):
    c = write_config(tmp_path, "ux")
    assert c.secret_key("cloudflare_api_token") == "CLOUDFLARE_API_TOKEN"
    assert c.secret_key("tailscale_oauth_client_id") == "TAILSCALE_OAUTH_CLIENT_ID"
    assert c.secret_key("tailscale_oauth_client_secret") == "TAILSCALE_OAUTH_CLIENT_SECRET"
    assert c.secret_key("state_passphrase") == "STATE_PASSPHRASE"
    assert c.secret_key("bot_private_key") == "SEALEDSTACK_BOT_PRIVATE_KEY"
    # registry-style upper-case names resolve to the same logical secret
    assert c.secret_key("CLOUDFLARE_API_TOKEN") == "CLOUDFLARE_API_TOKEN"
    assert c.secret_key("HCLOUD_TOKEN") == "HCLOUD_TOKEN"


def test_a_renamed_key_is_honoured_for_either_spelling_of_the_logical_name(tmp_path):
    c = write_config(tmp_path, "rn", keys={"state_passphrase": "MY_PASSPHRASE", "CLOUDFLARE_API_TOKEN": "CF_TOKEN"})
    assert c.secret_key("state_passphrase") == "MY_PASSPHRASE"
    assert c.secret_key("STATE_PASSPHRASE") == "MY_PASSPHRASE"
    assert c.secret_key("cloudflare_api_token") == "CF_TOKEN"
    source = EnvSource({"MY_PASSPHRASE": "0123456789abcdef"})
    assert tofu.require(source, c, "state_passphrase") == "0123456789abcdef"


def test_the_deprecated_state_passphrase_key_still_works_with_a_warning(tmp_path, capsys):
    secrets_mod._WARNED.clear()
    c = write_config(tmp_path, "dep")
    source = EnvSource({"state_passphrase": "0123456789abcdef"})
    result = secrets_mod.lookup(source, c, "state_passphrase")
    assert result.found and result.value == "0123456789abcdef"
    err = capsys.readouterr().err
    assert "deprecated" in err and "STATE_PASSPHRASE" in err and "0123456789abcdef" not in err


def test_the_default_key_wins_over_the_deprecated_one(tmp_path, capsys):
    secrets_mod._WARNED.clear()
    c = write_config(tmp_path, "dep2")
    source = EnvSource({"STATE_PASSPHRASE": "new-passphrase-123456", "state_passphrase": "old-passphrase-123456"})
    assert secrets_mod.lookup(source, c, "state_passphrase").value == "new-passphrase-123456"
    assert capsys.readouterr().err == ""


def test_no_fallback_when_config_renames_the_key(tmp_path):
    c = write_config(tmp_path, "dep3", keys={"state_passphrase": "MY_PASSPHRASE"})
    assert not secrets_mod.lookup(EnvSource({"state_passphrase": "0123456789abcdef"}), c, "state_passphrase").found


def test_check_reports_the_deprecated_key_as_a_warning_with_the_fix(tmp_path):
    secrets_mod._WARNED.clear()
    c = write_config(tmp_path, "dep4")
    ctx = preflight.build_context(c, env={}, secrets=EnvSource({"state_passphrase": "0123456789abcdef"}), version="0.5.0")
    r = {x.id: x for x in preflight.check_secrets(ctx)}["secrets.state_passphrase"]
    assert r.status == "warn"
    assert "STATE_PASSPHRASE" in r.fix and "deprecated" in r.detail


def test_messages_name_both_logical_and_source_key(tmp_path):
    c = write_config(tmp_path, "msg", keys={"state_passphrase": "MY_PASSPHRASE"})
    ctx = preflight.build_context(c, env={}, secrets=EnvSource({}), version="0.5.0")
    r = {x.id: x for x in preflight.check_secrets(ctx)}["secrets.state_passphrase"]
    assert r.status == "fail"
    assert "state_passphrase (source key MY_PASSPHRASE)" in r.detail
    with pytest.raises(cfg.ConfigError) as err:
        tofu.require(EnvSource({}), c, "state_passphrase")
    assert "state_passphrase (source key MY_PASSPHRASE)" in str(err.value)


def test_secrets_project_selects_the_bitwarden_project(tmp_path):
    c = write_config(tmp_path, "proj")
    c.stack["slots"]["secrets"] = "bitwarden"
    c.stack["secrets"]["project"] = "my-secrets"
    source = secrets_mod.build_source(c.stack, c.config_dir, {"BWS_ACCESS_TOKEN": "t"})
    assert source.project == "my-secrets"
    assert c.source_project() == "my-secrets"
    # app secrets use the configured app project, default 'prod'
    assert c.app_project() == "prod"
    c.stack["secrets"]["app_project"] = "apps-prod"
    assert c.app_project() == "apps-prod"
    app_source = secrets_mod.build_source(c.stack, c.config_dir, {"BWS_ACCESS_TOKEN": "t"}, project=c.app_project())
    assert app_source.project == "apps-prod"


def test_default_secrets_project_is_infra(tmp_path):
    c = write_config(tmp_path, "proj2")
    c.stack["slots"]["secrets"] = "bitwarden"
    c.stack["secrets"].pop("project", None)
    assert secrets_mod.build_source(c.stack, c.config_dir, {"BWS_ACCESS_TOKEN": "t"}).project == "infra"


def test_netcup_without_the_adapter_needs_no_api_credentials(tmp_path):
    servers = {"servers": {"box-1": {"provider": "netcup", "role": "control", "os": "ubuntu-24.04"}}}
    c = write_config(tmp_path, "nc", servers=servers)
    _, registry_mod, _, _ = preflight._load_script_modules(REPO)
    needed = preflight.gather_needed_secrets(c.stack, c.servers, registry_mod.load(), c.config_dir)
    assert "NETCUP_API_USERNAME" not in [n for n, _ in needed]
    # compute apply does not require them either
    compute.tf_vars(c, {}, EnvSource({}), "box-1")


def test_netcup_with_the_adapter_enabled_requires_them(tmp_path):
    servers = {"servers": {"box-1": {"provider": "netcup", "role": "control", "os": "ubuntu-24.04"}}}
    c = write_config(tmp_path, "nc2", servers=servers)
    (c.config_dir / "providers").mkdir()
    (c.config_dir / "providers" / "netcup.yaml").write_text("base_url: https://example.invalid\n")
    _, registry_mod, _, _ = preflight._load_script_modules(REPO)
    needed = [n for n, _ in preflight.gather_needed_secrets(c.stack, c.servers, registry_mod.load(), c.config_dir)]
    assert "NETCUP_API_USERNAME" in needed and "NETCUP_API_PASSWORD" in needed
    with pytest.raises(cfg.ConfigError):
        compute.tf_vars(c, {}, EnvSource({}), "box-1")
    compute.tf_vars(c, {}, EnvSource({"NETCUP_API_USERNAME": "u", "NETCUP_API_PASSWORD": "p"}), "box-1")


def test_app_secrets_keep_app_dot_name_and_can_be_remapped(tmp_path):
    c = write_config(tmp_path, "app", apps={"apps": {"shop": {"secrets": {"A": "custom.A"}}}})
    assert c.app_secret_key("shop", "A") == "custom.A"
    assert c.app_secret_key("shop", "B") == "shop.B"
