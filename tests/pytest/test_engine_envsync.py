"""env-sync merge logic (Task 4.9 / D-047), mirroring the old bats N9 cases."""
from __future__ import annotations

from sealedstack.envsync import merge_env


def test_keeps_app_version_and_unknown_keys_overwrites_infra():
    merged, unknown = merge_env("APP_VERSION=v1.2.3\nDATABASE_URL=old\nHAND_SET=keep", {"DATABASE_URL": "new"}, "demo")
    assert "APP_VERSION=v1.2.3" in merged
    assert "DATABASE_URL=new" in merged
    assert "HAND_SET=keep" in merged
    assert "DATABASE_URL=old" not in merged
    assert unknown == ["HAND_SET"]


def test_empty_current():
    merged, unknown = merge_env("", {"DATABASE_URL": "new"}, "demo")
    assert merged == "DATABASE_URL=new"
    assert unknown == []


def test_keeps_comments_and_order():
    merged, _ = merge_env(
        "# release-owned\nAPP_VERSION=v1.2.3\n# infra-owned\nDATABASE_URL=old",
        {"DATABASE_URL": "new"},
        "demo",
    )
    assert merged.split("\n") == ["# release-owned", "APP_VERSION=v1.2.3", "# infra-owned", "DATABASE_URL=new"]


def test_multiline_pem_roundtrips():
    current = 'APP_VERSION=v1\nPEM="-----BEGIN KEY-----\nabc\n-----END KEY-----"'
    merged, _ = merge_env(current, {"DATABASE_URL": "new"}, "demo")
    assert 'PEM="-----BEGIN KEY-----\nabc\n-----END KEY-----"' in merged
    assert "DATABASE_URL=new" in merged


def test_infra_value_newlines_escaped_quoted():
    merged, _ = merge_env("APP_VERSION=v1", {"PEM": "line1\nline2"}, "demo")
    assert 'PEM="line1\\nline2"' in merged
    assert "APP_VERSION=v1" in merged


def test_infra_value_with_spaces_quoted():
    merged, _ = merge_env("APP_VERSION=v1", {"GREETING": "hello world"}, "demo")
    assert 'GREETING="hello world"' in merged


# -- apps with their own Dokploy secrets provider: references only ---------------------
import io
import json
from types import SimpleNamespace

import pytest

from sealedstack import buildenv, envsync


class FakeClient:
    def __init__(self, providers, current=""):
        self.providers, self.current, self.updated = providers, current, {}

    def vault_providers(self):
        return self.providers

    def compose_env(self, compose_id):
        return self.current

    def compose_update(self, compose_id, env):
        self.updated[compose_id] = env


class FakeState:
    def __init__(self, app_env, compose_ids):
        self.values = {"app_env": app_env, "compose_ids": compose_ids}

    def output(self, name):
        return self.values[name]


def vault_app(tmp_path, data):
    apps = tmp_path / ".build" / "apps"
    apps.mkdir(parents=True)
    (apps / "paisa.json").write_text(json.dumps(data))
    return SimpleNamespace(code_root=tmp_path)


def run_sync(config, client, app_env=None):
    state = FakeState({"paisa": app_env or {}}, {"paisa": {"web": "c1"}})
    envsync.run(config, {}, client=client, state=state, source=object(), out=io.StringIO())


def test_vault_references_are_written_never_values(tmp_path):
    config = vault_app(tmp_path, {"name": "paisa", "vault": {"provider": "paisa", "keys": ["CLERK_SECRET_KEY"]}})
    client = FakeClient([{"name": "paisa"}], current="APP_VERSION=v1")
    run_sync(config, client)
    assert client.updated["c1"] == "APP_VERSION=v1\nCLERK_SECRET_KEY=${{vault.paisa.CLERK_SECRET_KEY}}"


def test_a_missing_provider_stops_before_anything_is_written(tmp_path):
    config = vault_app(tmp_path, {"name": "paisa", "vault": {"provider": "paisa", "keys": ["K"]}})
    client = FakeClient([{"name": "other"}])
    with pytest.raises(envsync.ProviderMissing, match="no secrets provider named 'paisa'"):
        run_sync(config, client)
    assert client.updated == {}


def test_an_old_dokploy_without_the_router_only_warns(tmp_path):
    config = vault_app(tmp_path, {"name": "paisa", "vault": {"provider": "paisa", "keys": ["K"]}})

    class Old(FakeClient):
        def vault_providers(self):
            raise RuntimeError("HTTP 404")

    client = Old([])
    run_sync(config, client)
    assert "K=${{vault.paisa.K}}" in client.updated["c1"]


def test_the_object_form_is_not_resolved_by_build_env():
    assert buildenv.app_secret_keys({"manifest": {"secrets": {"provider": "p", "keys": ["A"]}}}) == []
    assert buildenv.app_secret_keys({"manifest": {"secrets": ["A", "B"]}}) == ["A", "B"]
