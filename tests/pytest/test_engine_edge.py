"""edge + tofu backend port (Task 4.9 / D-047)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, edge, secrets as secrets_mod
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


def make_config(tmp_path: Path) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "cloudflare": {"account_id": "acct123", "zone_id": "zone123"},
                "state": {"bucket": "example-state"},
                "slots": {"secrets": "bitwarden"},
            }
        )
    )
    (cfg_dir / "apps.yaml").write_text("{}\n")
    (cfg_dir / "servers.yaml").write_text("servers: {}\n")
    (tmp_path / "tofu" / "stacks" / "edge").mkdir(parents=True)
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


REQUIRED = {
    "CLOUDFLARE_API_TOKEN": "cf",
    "TAILSCALE_OAUTH_CLIENT_ID": "id",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
    # explicit R2 override: no token-verify request in unit tests (D-054)
    "R2_ACCESS_KEY_ID": "rid",
    "R2_SECRET_ACCESS_KEY": "rsecret",
}


def test_tfvars_requires_cloudflare_token(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(config_mod.ConfigError):
        edge.tf_vars(config, {"BWS_ACCESS_TOKEN": "t"}, FakeSource({}))


def test_tfvars_backup_defaults_from_account(tmp_path):
    config = make_config(tmp_path)
    source = FakeSource({**REQUIRED, "R2_ACCESS_KEY_ID": "rac", "R2_SECRET_ACCESS_KEY": "rsec"})
    variables = edge.tf_vars(config, {"BWS_ACCESS_TOKEN": "t"}, source)
    assert variables["TF_VAR_backup_endpoint"] == "https://acct123.r2.cloudflarestorage.com"
    assert variables["TF_VAR_backup_access_key_id"] == "rac"
    assert variables["TF_VAR_backup_region"] == "auto"


def test_edge_apply_writes_outputs(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(edge.tofu_mod, "ready", lambda *a, **k: None)
    ran = []

    def runner(argv):
        ran.append(argv)
        return 0

    rc = edge.run(
        config,
        {"BWS_ACCESS_TOKEN": "t"},
        "apply",
        source=FakeSource(REQUIRED),
        runner=runner,
        output_runner=lambda argv: (0, json.dumps({"tunnel": {"edge": "connector-token"}})),
    )

    assert rc == 0
    assert ran[0][-1] == "apply"
    written = json.loads((tmp_path / ".build" / "edge-outputs.json").read_text())
    assert written["tunnel"]["edge"] == "connector-token"
