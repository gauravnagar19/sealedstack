"""platform port (Task 4.9 / D-047)."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, platform, secrets as secrets_mod
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


def make_config(tmp_path: Path, backup: dict | None = None) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "admin_user": "admin",
                "cloudflare": {"account_id": "acct123"},
                "mesh": {"tailnet": "tail1234.ts.net"},
                "backup": backup or {},
                "slots": {"secrets": "bitwarden"},
            }
        )
    )
    (cfg_dir / "servers.yaml").write_text(
        yaml.safe_dump({"servers": {"control-1": {"role": "control", "provider": "hetzner"}}})
    )
    (cfg_dir / "apps.yaml").write_text("{}\n")
    (tmp_path / "tofu" / "stacks" / "platform").mkdir(parents=True)
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})

from fake_generated import fake_generated  # noqa: E402


def test_control_host_uses_tailnet(tmp_path):
    config = make_config(tmp_path)
    assert platform.control_host(config) == "control-1.tail1234.ts.net"


def test_tfvars_requires_dokploy_key(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(config_mod.ConfigError, match="sealedstack up"):
        platform.tf_vars(config, {}, FakeSource({}), generated=fake_generated())


def test_tfvars_endpoint_from_control_host(tmp_path):
    config = make_config(tmp_path)
    source = FakeSource({"DOKPLOY_API_KEY": "dk"})
    variables = platform.tf_vars(config, {}, source, generated=fake_generated())
    assert variables["TF_VAR_dokploy_endpoint"] == "http://control-1.tail1234.ts.net:3000"
    assert variables["TF_VAR_admin_user"] == "admin"


def test_tfvars_secondary_backup_required_when_configured(tmp_path):
    config = make_config(tmp_path, backup={"secondary": "https://backup2.example.com"})
    source = FakeSource({"DOKPLOY_API_KEY": "dk"})
    with pytest.raises(config_mod.ConfigError):
        platform.tf_vars(config, {}, source, generated=fake_generated())
