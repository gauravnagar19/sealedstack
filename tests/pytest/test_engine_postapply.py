"""Dokploy client + post-apply orchestration (Task 4.9 / D-047)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, deployapps, dokploy, envsync, postapply
from sealedstack.http import Route, StubClient


def test_dokploy_client_uses_x_api_key_and_raises_on_error():
    http = StubClient()
    http.add("/api/compose.one", Route(json_body={"env": "APP_VERSION=v1"}))
    client = dokploy.DokployClient("http://dokploy.example.com:3000/", "secret", http=http)

    assert client.compose_env("c1") == "APP_VERSION=v1"
    assert http.requests[0]["headers"]["x-api-key"] == "secret"
    assert http.requests[0]["url"] == "http://dokploy.example.com:3000/api/compose.one?composeId=c1"


def test_dokploy_client_error_raises():
    http = StubClient()  # any request -> 404
    client = dokploy.DokployClient("http://x:3000", "k", http=http)
    with pytest.raises(dokploy.DokployError):
        client.compose("c9")


def make_config(tmp_path: Path) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(yaml.safe_dump({"name": "example-infra", "slots": {"secrets": "env"}}))
    (cfg_dir / "apps.yaml").write_text("{}\n")
    (cfg_dir / "servers.yaml").write_text("servers: {}\n")
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


def test_postapply_restores_then_syncs_deploys_and_cleans(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    apps = tmp_path / ".build" / "apps"
    apps.mkdir(parents=True)
    (apps / "shop.json").write_text(
        json.dumps({"name": "shop", "manifest": {"needs": {"postgres": {"restore_on_create": True}}}})
    )
    (tmp_path / ".build" / "env.json").write_text('{"shop": {}}')
    (tmp_path / ".build" / "edge-outputs.json").write_text("{}")

    calls: list[str] = []
    monkeypatch.setattr(envsync, "run", lambda *a, **k: calls.append("env-sync"))
    monkeypatch.setattr(deployapps, "run", lambda *a, **k: calls.append("deploy-apps"))

    restored: list[tuple] = []

    class FakeDb:
        def restore(self, app, key="", *, force=False, restore_on_create=False):
            restored.append((app, restore_on_create))
            return 0

    postapply.run(config, {}, db=FakeDb(), out=io.StringIO())

    assert calls == ["env-sync", "deploy-apps"]
    assert restored == [("shop", True)]
    assert not (tmp_path / ".build" / "env.json").exists()
    assert not (tmp_path / ".build" / "edge-outputs.json").exists()
