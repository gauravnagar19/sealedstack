"""D-061 / DR-4: first login is by root password only; `bootstrap.method: key` is rejected; no interactive prompt."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, preflight, server
from test_engine_preflight import APPS, STACK, by_id, make_config as make_preflight_config, make_ctx
from test_engine_server import FakeIO, FakeSource, fake_generated, make_config

REPO = Path(__file__).resolve().parents[2]
MESSAGE = "bootstrap.method: key is not implemented yet; use password (D-061)"
SECRETS = {"SERVER_IP_WEB_1": "203.0.113.10", "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}


def with_method(config, method):
    config.servers["servers"]["web-1"]["bootstrap"] = {"method": method}
    return config


# -- check / validation -------------------------------------------------------------------
def test_check_rejects_method_key_with_the_exact_message(tmp_path):
    servers = {"servers": {"web-1": {"provider": "netcup", "role": "control", "os": "ubuntu-24.04", "bootstrap": {"method": "key"}}}}
    config = make_preflight_config(tmp_path, servers=servers)
    row = by_id(preflight.check_config(make_ctx(tmp_path, config)))["config.cross_file"]
    assert row.status == "fail" and f"server 'web-1': {MESSAGE}" in row.detail


def test_method_password_and_the_default_pass(tmp_path):
    for bootstrap in ({"method": "password"}, {}):
        servers = {"servers": {"web-1": {"provider": "netcup", "role": "control", "os": "ubuntu-24.04", "bootstrap": bootstrap}}}
        config = make_preflight_config(tmp_path / str(len(bootstrap)), servers=servers)
        assert by_id(preflight.check_config(make_ctx(tmp_path, config)))["config.cross_file"].status == "ok"


def test_the_schema_still_lists_key_for_forward_compatibility():
    import json

    schema = json.loads((REPO / "schemas" / "servers.schema.json").read_text())
    entry = next(iter(schema["properties"]["servers"]["patternProperties"].values()))
    assert entry["properties"]["bootstrap"]["properties"]["method"]["enum"] == ["password", "key"]


# -- server up ----------------------------------------------------------------------------------
def test_up_refuses_method_key_before_doing_anything(tmp_path):
    io = FakeIO()
    with pytest.raises(config_mod.ConfigError, match=r"server 'web-1': bootstrap\.method: key is not implemented yet; use password \(D-061\)"):
        server.up(with_method(make_config(tmp_path), "key"), {}, "web-1", source=FakeSource({**SECRETS, "ROOT_PASSWORD_WEB_1": "pw"}), io=io, generated=fake_generated())
    assert io.runs == [] and io.captures == []


def test_a_missing_root_password_is_a_clear_error_not_a_prompt(tmp_path):
    """Only the IP is in the source: the old code would have hung the job waiting for a password."""
    io = FakeIO()
    with pytest.raises(config_mod.ConfigError) as err:
        server.up(make_config(tmp_path), {}, "web-1", source=FakeSource(SECRETS), io=io, generated=fake_generated())
    assert "ROOT_PASSWORD_WEB_1" in str(err.value)
    assert not any("--ask-pass" in " ".join(run) for run in io.runs)


def test_a_created_server_reads_the_provider_issued_password_from_the_source(tmp_path):
    io = FakeIO(captures={"python": "203.0.113.50"})
    io.capture = lambda argv, env: (0, "203.0.113.50" if "sealedstack.compute" in " ".join(argv) else "tskey-auth-x")
    config = make_config(tmp_path, provider="hetzner")
    source = FakeSource({"TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec", "ROOT_PASSWORD_WEB_1": "emailed-pw"})
    server.up(config, {}, "web-1", source=source, io=io, generated=fake_generated())
    assert io.vars_files[0]["ansible_password"] == "emailed-pw"
    assert not any("--ask-pass" in " ".join(run) for run in io.runs)


def test_a_created_server_without_any_password_fails_with_the_hetzner_hint(tmp_path):
    io = FakeIO()
    io.capture = lambda argv, env: (0, "203.0.113.50" if "sealedstack.compute" in " ".join(argv) else "tskey-auth-x")
    config = make_config(tmp_path, provider="hetzner")
    source = FakeSource({"TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"})
    with pytest.raises(config_mod.ConfigError, match=r"no root password for 'web-1'.*ROOT_PASSWORD_WEB_1.*Hetzner e-mails.*never prompts"):
        server.up(config, {}, "web-1", source=source, io=io, generated=fake_generated())
    assert io.runs == []  # ansible never ran, nothing hung


def test_the_env_override_for_a_local_run_still_works(tmp_path):
    io = FakeIO()
    server.up(make_config(tmp_path), {"ROOT_PASSWORD": "from-env"}, "web-1",
              source=FakeSource({"SERVER_IP_WEB_1": "203.0.113.10", "ROOT_PASSWORD_WEB_1": "x", "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}),
              io=io, generated=fake_generated())
    assert io.vars_files[0]["ansible_password"] == "x"


def test_no_ask_pass_remains_in_the_engine():
    assert "ask-pass" not in (REPO / "engine" / "sealedstack" / "server.py").read_text()
