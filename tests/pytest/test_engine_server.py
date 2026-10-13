"""server up/sync port (Task 4.9 / D-047)."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, secrets as secrets_mod, server
from sealedstack.secrets import SecretResult

REPO = Path(__file__).resolve().parents[2]


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


class FakeIO(server.ServerIO):
    def __init__(self, captures=None):
        self.runs: list[list] = []
        self.captures: list[list] = []
        self.prompts: list[str] = []
        self.vars_files: list[dict] = []
        self._captures = captures or {}

    def _match(self, argv):
        for needle, out in self._captures.items():
            if argv and needle in argv[0]:
                return out
        return ""

    def run(self, argv, env):
        self.runs.append(list(argv))
        # Record the -e @vars files while they still exist (they are 0600 temp files).
        for i, arg in enumerate(argv):
            if arg == "-e" and i + 1 < len(argv) and argv[i + 1].startswith("@"):
                self.vars_files.append(json.loads(Path(argv[i + 1][1:]).read_text()))
        return 0

    def capture(self, argv, env):
        self.captures.append(list(argv))
        return 0, self._match(argv)

    def prompt(self, message):
        self.prompts.append(message)
        return ""


def make_config(tmp_path: Path, provider: str = "netcup") -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "admin_user": "admin",
                "admin": {"ssh_keys": ["ssh-ed25519 AAAA you@example.com"]},
                "slots": {"secrets": "bitwarden", "mesh": "tailscale"},
            }
        )
    )
    (cfg_dir / "servers.yaml").write_text(
        yaml.safe_dump({"servers": {"web-1": {"provider": provider, "role": "control"}}})
    )
    (cfg_dir / "apps.yaml").write_text("{}\n")
    return config_mod.load_config(config_dir=cfg_dir, code_root=REPO, env={})


def test_creates_server_reads_registry():
    assert server.creates_server(REPO, "hetzner") is True
    assert server.creates_server(REPO, "netcup") is False


def test_write_vars_drops_empty_and_is_0600(tmp_path):
    path = tmp_path / "vars.json"
    server.write_vars(path, {"a": "1", "b": ""})
    assert json.loads(path.read_text()) == {"a": "1"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_up_existing_machine_bootstraps_then_sites(tmp_path, capsys):
    config = make_config(tmp_path, provider="netcup")
    io = FakeIO(
        captures={
            "sealedstack.tailscalekey": "tskey-auth-xyz",
            "sealedstack.dokploykey": "ssh-ed25519 BBBB dokploy",
        }
    )
    source = FakeSource(
        {
            "SERVER_IP_WEB_1": "203.0.113.10",
            "ROOT_PASSWORD_WEB_1": "rootpw",
            "TAILSCALE_OAUTH_CLIENT_ID": "id",
            "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
        }
    )

    msg = server.up(config, {}, "web-1", source=source, io=io)

    joined = [" ".join(r) for r in io.runs]
    assert any("bootstrap.yml" in x and "-u root" in x for x in joined)
    assert any("site.yml" in x and "--limit web-1" in x for x in joined)
    # netcup firewall is manual -> prompt, no compute call
    assert io.prompts and not any("sealedstack.compute" in x for x in joined)
    assert "web-1" in msg


def test_up_needs_no_sudo_password_and_passes_none_to_ansible(tmp_path):
    """D-055: no admin_sudo_password secret; no sudo/become password reaches Ansible."""
    config = make_config(tmp_path)
    io = FakeIO(captures={"sealedstack.tailscalekey": "tskey", "sealedstack.dokploykey": "ssh-ed25519 BBBB dokploy"})
    source = FakeSource({"SERVER_IP_WEB_1": "203.0.113.10", "ROOT_PASSWORD_WEB_1": "rootpw",
                         "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"})
    server.up(config, {}, "web-1", source=source, io=io)
    assert len(io.vars_files) == 2  # bootstrap + site
    for vars_file in io.vars_files:
        assert "admin_password" not in vars_file and "ansible_become_password" not in vars_file
    assert io.vars_files[0]["ansible_password"] == "rootpw"  # the one-time root login only


def test_sync_passes_only_public_keys(tmp_path):
    config = make_config(tmp_path)
    io = FakeIO()
    assert server.sync(config, {}, "web-1", source=FakeSource({}), io=io) == 0
    assert io.vars_files == [{"admin_ssh_keys_json": json.dumps(["ssh-ed25519 AAAA you@example.com"])}]


def test_up_without_bootstrap_secrets_only_reasserts_the_baseline(tmp_path, capsys):
    """D-049: the secrets are deleted after the first bootstrap, so `up` must be re-runnable."""
    config = make_config(tmp_path, provider="netcup")
    io = FakeIO(captures={"sealedstack.dokploykey": "ssh-ed25519 BBBB dokploy"})
    source = FakeSource({})  # no SERVER_IP_/ROOT_PASSWORD_

    msg = server.up(config, {}, "web-1", source=source, io=io)

    joined = [" ".join(r) for r in io.runs]
    assert not any("bootstrap.yml" in x for x in joined)  # no public-SSH bootstrap
    assert any("site.yml" in x and "--limit web-1" in x for x in joined)
    assert not any("sealedstack.tailscalekey" in " ".join(c) for c in io.captures)  # no auth key minted
    assert not io.prompts  # nothing changed at the provider firewall
    assert "already bootstrapped" in capsys.readouterr().out and "web-1" in msg


def test_up_with_only_one_bootstrap_secret_is_an_error(tmp_path):
    config = make_config(tmp_path, provider="netcup")
    source = FakeSource({"SERVER_IP_WEB_1": "203.0.113.10"})
    with pytest.raises(config_mod.ConfigError, match="ROOT_PASSWORD_WEB_1"):
        server.up(config, {}, "web-1", source=source, io=FakeIO())


def test_up_site_failure_without_secrets_names_the_missing_keys(tmp_path):
    class FailingIO(FakeIO):
        def run(self, argv, env):
            super().run(argv, env)
            return 4

    config = make_config(tmp_path, provider="netcup")
    io = FailingIO(captures={"sealedstack.dokploykey": "ssh-ed25519 BBBB dokploy"})
    with pytest.raises(config_mod.ConfigError, match="SERVER_IP_WEB_1 and ROOT_PASSWORD_WEB_1"):
        server.up(config, {}, "web-1", source=FakeSource({}), io=io)


def test_default_prompt_does_not_crash_without_a_terminal(monkeypatch, capsys):
    import io as stdio

    monkeypatch.setattr(server.sys, "stdin", stdio.StringIO(""))  # not a tty, EOF on read
    assert server._default_io().prompt("Press Enter when done… ") == ""
    assert "non-interactive" in capsys.readouterr().out
