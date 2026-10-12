"""deploy-apps unit logic (Task 4.9 / D-047)."""
from __future__ import annotations

import pytest

from sealedstack import deployapps


def test_set_app_version_replaces_and_appends():
    assert deployapps.set_app_version("APP_VERSION=v1\nFOO=bar", "v2") == "APP_VERSION=v2\nFOO=bar"
    assert deployapps.set_app_version("FOO=bar", "v2") == "FOO=bar\nAPP_VERSION=v2"


def test_app_hash_changes_with_ids_and_env():
    rendered = {"source": {"repo": "o/r"}, "hostnames": ["a.example.com"], "_compose_hash": "h"}
    base = deployapps.app_hash(rendered, {"A": "1"}, ["id1"])
    assert base == deployapps.app_hash(rendered, {"A": "1"}, ["id1"])
    assert base != deployapps.app_hash(rendered, {"A": "1"}, ["id2"])
    assert base != deployapps.app_hash(rendered, {"A": "2"}, ["id1"])


def test_last_deploy_status_picks_newest():
    client = _FakeClient([[
        {"deploymentId": "a", "status": "error", "createdAt": "2024-01-01"},
        {"deploymentId": "b", "status": "done", "createdAt": "2024-01-02"},
    ]])
    assert deployapps.last_deploy_status(client, "c1") == "done"


def test_poll_deploy_waits_for_new_done():
    client = _FakeClient(
        [
            [{"deploymentId": "old", "status": "done", "createdAt": "1"}],
            [{"deploymentId": "old", "status": "done", "createdAt": "1"}, {"deploymentId": "new", "status": "running", "createdAt": "2"}],
            [{"deploymentId": "new", "status": "done", "createdAt": "2"}],
        ]
    )
    result = deployapps.poll_deploy(client, "c1", ["old"], sleep=lambda _: None, tries=5, interval=0)
    assert result == "new:done"


def test_poll_deploy_error_raises():
    client = _FakeClient([[{"deploymentId": "new", "status": "error", "createdAt": "2"}]])
    with pytest.raises(deployapps.DeployError):
        deployapps.poll_deploy(client, "c1", [], sleep=lambda _: None, tries=3, interval=0)


def test_released_version_precedence():
    assert deployapps.released_version("shop", "o/r", {"SEALEDSTACK_RELEASED_VERSION_SHOP": "v9"}) == "v9"
    assert deployapps.released_version("shop", "o/r", {"SEALEDSTACK_RELEASED_VERSION": "v8"}) == "v8"
    assert deployapps.released_version("shop", "o/r", {}) == ""


class _FakeClient:
    def __init__(self, deployments_sequence):
        self._seq = list(deployments_sequence)
        self._last = deployments_sequence[-1] if deployments_sequence else []
        self.envs: dict[str, str] = {}

    def deployments(self, compose_id):
        return self._seq.pop(0) if self._seq else self._last

    def compose_env(self, compose_id):
        return self.envs.get(compose_id, "")

    def compose_update(self, compose_id, env):
        self.envs[compose_id] = env

    def compose_deploy(self, compose_id):
        return None
