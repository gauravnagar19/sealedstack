"""D-050: setup/init write the explicit secret-name defaults into config."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
for extra in (REPO / "engine", REPO / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from sealedstack import config as cfg  # noqa: E402
from sealedstack import scaffold  # noqa: E402

import validate as validate_mod  # noqa: E402


def schema_ok(doc: dict, name: str) -> list[str]:
    return validate_mod.schema_errors(doc, REPO / "schemas" / f"{name}.schema.json")


def test_stack_config_writes_every_core_secret_key_explicitly():
    stack = scaffold.stack_config(infra_name="my-infra", domain="example.com", owner_email="you@example.com",
                                  account_id="acct123", tailnet="tail1234.ts.net")
    assert stack["secrets"]["keys"] == {
        "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
        "tailscale_oauth_client_id": "TAILSCALE_OAUTH_CLIENT_ID",
        "tailscale_oauth_client_secret": "TAILSCALE_OAUTH_CLIENT_SECRET",
        "state_passphrase": "STATE_PASSPHRASE",
        "bot_private_key": "SEALEDSTACK_BOT_PRIVATE_KEY",
    }
    assert stack["secrets"]["project"] == "infra"
    assert stack["secrets"]["app_project"] == "prod"
    assert schema_ok(stack, "stack") == []


def test_existing_machine_servers_get_explicit_bootstrap_names():
    servers = scaffold.servers_config([("server-1", "netcup", "control", "ubuntu-24.04"), ("w-2", "hetzner", "worker", "ubuntu-24.04")])
    one = servers["servers"]["server-1"]
    assert one["bootstrap"] == {"method": "password", "ip_secret": "SERVER_IP_SERVER_1", "password_secret": "ROOT_PASSWORD_SERVER_1"}
    assert "bootstrap" not in servers["servers"]["w-2"]  # API providers need no IP or password
    assert schema_ok(servers, "servers") == []


def test_written_defaults_resolve_to_themselves_and_renames_work(tmp_path):
    stack = scaffold.stack_config(infra_name="my-infra", domain="example.com", owner_email="you@example.com")
    stack["secrets"]["keys"]["cloudflare_api_token"] = "MY_CF"
    c = cfg.Config(REPO, tmp_path, stack, {}, {})
    assert c.secret_key("cloudflare_api_token") == "MY_CF"
    assert c.secret_key("state_passphrase") == "STATE_PASSPHRASE"


def test_the_wizard_writes_the_defaults(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("sealedstack_init_wizard", REPO / "scripts" / "init.py")
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)

    answers = iter(["example.com", "you@example.com", "admin", "", "acct123", "example", "tail1234.ts.net",
                    "server-1", "netcup", "control", "ubuntu-24.04", ""])
    monkeypatch.setattr(init, "ask", lambda prompt, default="": next(answers) or default)
    monkeypatch.setattr(init, "fetch_tailscale_policy", lambda: "{}")
    monkeypatch.setattr(sys, "argv", ["init", "--infra-name", "my-infra", "--owner", "you", "--no-scaffold",
                                      "--config-dir", str(tmp_path / "config")])
    assert init.main() == 0

    stack = yaml.safe_load((tmp_path / "config" / "stack.yaml").read_text())
    servers = yaml.safe_load((tmp_path / "config" / "servers.yaml").read_text())
    assert stack["secrets"]["keys"]["state_passphrase"] == "STATE_PASSPHRASE"
    assert stack["approvers"] == ["you"]
    assert servers["servers"]["server-1"]["bootstrap"]["ip_secret"] == "SERVER_IP_SERVER_1"
    assert schema_ok(stack, "stack") == [] and schema_ok(servers, "servers") == []
