"""D-077: the root password for the provider console comes from the secret source and is applied by the baseline."""
from __future__ import annotations

import pytest
import yaml

from sealedstack import config as config_mod, server
from test_engine_server import REPO, FakeIO, FakeSource, fake_generated, make_config

SECRETS = {"SERVER_IP_WEB_1": "203.0.113.10", "ROOT_PASSWORD_WEB_1": "bootstrap-password-1",
           "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}
CONSOLE = "a-console-password-0123456789"
ROLE = (REPO / "ansible" / "roles" / "base" / "tasks" / "main.yml").read_text(encoding="utf-8")


def test_the_key_defaults_to_console_password_for_the_server_and_can_be_renamed(tmp_path):
    config = make_config(tmp_path)
    assert config.console_password_secret("web-1") == "CONSOLE_PASSWORD_WEB_1"
    servers = yaml.safe_load((config.config_dir / "servers.yaml").read_text())
    servers["servers"]["web-1"]["bootstrap"] = {"console_password_secret": "MY_CONSOLE_PW"}
    (config.config_dir / "servers.yaml").write_text(yaml.safe_dump(servers))
    renamed = config_mod.load_config(config_dir=config.config_dir, code_root=REPO, env={})
    assert renamed.console_password_secret("web-1") == "MY_CONSOLE_PW"


def test_up_hands_the_console_password_to_the_baseline_but_not_to_the_bootstrap_play(tmp_path):
    config, io = make_config(tmp_path, provider="netcup"), FakeIO(captures={"sealedstack.tailscalekey": "tskey"})
    server.up(config, {}, "web-1", source=FakeSource({**SECRETS, "CONSOLE_PASSWORD_WEB_1": CONSOLE}), io=io, generated=fake_generated())
    bootstrap_vars, site_vars = io.vars_files
    assert "root_console_password" not in bootstrap_vars  # the bootstrap play logs in with the one-time password only
    assert site_vars["root_console_password"] == CONSOLE


def test_without_the_secret_nothing_is_passed_and_the_root_password_is_left_alone(tmp_path):
    config, io = make_config(tmp_path, provider="netcup"), FakeIO(captures={"sealedstack.tailscalekey": "tskey"})
    server.up(config, {}, "web-1", source=FakeSource(SECRETS), io=io, generated=fake_generated())
    assert all("root_console_password" not in v for v in io.vars_files)


@pytest.mark.parametrize("bad", ["short", "has\nnewline-in-it-123456", "carriage\rreturn-in-it-123456"])
def test_a_weak_or_multi_line_password_is_refused_without_echoing_it(tmp_path, bad):
    config = make_config(tmp_path, provider="netcup")
    with pytest.raises(config_mod.ConfigError) as err:
        server.console_password(config, FakeSource({"CONSOLE_PASSWORD_WEB_1": bad}), "web-1")
    assert "CONSOLE_PASSWORD_WEB_1" in str(err.value) and bad.strip() not in str(err.value)


def test_sync_also_applies_it(tmp_path):
    config, io = make_config(tmp_path), FakeIO()
    server.sync(config, {}, "web-1", source=FakeSource({"CONSOLE_PASSWORD_WEB_1": CONSOLE}), io=io, generated=fake_generated())
    assert io.vars_files[0]["root_console_password"] == CONSOLE


def test_the_baseline_task_never_logs_the_value_and_only_runs_when_it_changed():
    tasks = yaml.safe_load(ROLE)
    block = next(t for t in tasks if t.get("name") == "Set the root password for the provider console")
    assert "root_console_password" in block["when"]
    inner = {t["name"]: t for t in block["block"]}
    apply = inner["Apply the console password"]
    assert apply["ansible.builtin.command"] == "chpasswd" and apply["no_log"] is True
    assert "root:{{ root_console_password }}" in apply["args"]["stdin"]  # on stdin, never in argv
    assert "hash('sha256')" in apply["when"] and "base_console_marker" in apply["when"]
    assert inner["Record the console password marker"]["no_log"] is True
    assert inner["Create the sealedstack state directory"]["ansible.builtin.file"]["mode"] == "0700"
