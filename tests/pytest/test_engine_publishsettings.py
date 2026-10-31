"""publish-app-settings port (Task 4.9 / D-047), now via the forge (M7)."""
from __future__ import annotations

import io
from pathlib import Path

import yaml

from sealedstack import config as config_mod, forge, publishsettings, secrets as secrets_mod
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


class FakeState:
    def output(self, name):
        if name == "compose_ids":
            return {"shop": {"web-1": "compose-1"}}
        return {}


def make_config(tmp_path: Path) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "cloudflare": {"account_id": "acct"},
                "slots": {"secrets": "env"},
                "providers": {
                    "mesh_tailscale": {
                        "oauth_client_id": "ci-id",
                        "audience": "ci-aud",
                        "release_oauth_client_id": "rel-id",
                        "release_audience": "rel-aud",
                    }
                },
            }
        )
    )
    (cfg_dir / "apps.yaml").write_text(
        yaml.safe_dump({"apps": {"shop": {"source": {"repo": "example/shop"}}, "landing": {"source": {}}}})
    )
    (cfg_dir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"web-1": {"role": "control"}}}))
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "registry.json").write_text('{"image_prefix": "ghcr.io/example"}')
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


def test_publishes_vars_and_secret_for_repo_apps_only(tmp_path):
    config = make_config(tmp_path)
    f = forge.InMemoryForge()
    publishsettings.run(
        config,
        {},
        source=FakeSource({"DOKPLOY_API_KEY": "dk"}),
        state=FakeState(),
        forge=f,
        out=io.StringIO(),
    )

    repo = "example/shop"
    assert f.get_variable(repo, "SEALEDSTACK_COMPOSE_IDS") == '{"web-1":"compose-1"}'
    assert f.get_variable(repo, "SEALEDSTACK_DOKPLOY_URL") == "http://web-1:3000"
    # D-057: the release identity (tag:release) is published; the infra repo's tag:ci one is not
    assert f.get_variable(repo, "SEALEDSTACK_TS_RELEASE_CLIENT_ID") == "rel-id"
    assert f.get_variable(repo, "SEALEDSTACK_TS_RELEASE_AUDIENCE") == "rel-aud"
    assert f.get_variable(repo, "SEALEDSTACK_TS_OAUTH_CLIENT_ID") in (None, "")
    assert f.get_variable(repo, "SEALEDSTACK_TS_AUDIENCE") in (None, "")
    assert f.get_variable(repo, "SEALEDSTACK_REGISTRY") == "ghcr.io/example"
    assert f.secrets[(repo, "SEALEDSTACK_DOKPLOY_API_KEY")] == "dk"
    assert (repo, "production") in f.environments
    # the landing app has no repo, so nothing is published under it
    assert all(key[0] != "landing" for key in f.variables)
