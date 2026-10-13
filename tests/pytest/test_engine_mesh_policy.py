"""Mesh policy guard (N4/F1) — ported from tests/bats/mesh_policy_test.bats (no jq)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / ".build" / "tailscale-policy.json"


def _run(tmp_path: Path, stack: str, policy: str | None = None):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "stack.yaml").write_text(stack)
    if policy is not None:
        (cfg / "tailscale-policy.hujson").write_text(policy)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "mesh-policy.py"), "--config-dir", str(cfg)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )


def test_managed_policy_without_file_exits_1(tmp_path):
    OUT.unlink(missing_ok=True)
    result = _run(tmp_path, "slots: { mesh: tailscale }\nmesh: { policy: managed }\n")
    assert result.returncode == 1
    assert "is missing" in (result.stdout + result.stderr)


def test_untouched_sections_pass_through_and_rules_appended(tmp_path):
    policy = (
        "{\n"
        '  // the operator\'s own policy\n'
        '  "groups": { "group:admins": ["alice@example.com"] },\n'
        '  "ssh": [ { "action": "accept", "src": ["group:admins"], "dst": ["tag:server"], "users": ["root"] } ],\n'
        '  "nodeAttrs": [ { "target": ["tag:server"], "attr": ["funnel"] } ],\n'
        '  "grants": [ { "src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:3000"] } ],\n'
        "}\n"
    )
    result = _run(tmp_path, "slots: { mesh: tailscale }\nmesh: { policy: managed }\n", policy)
    assert result.returncode == 0, result.stderr
    data = json.loads(OUT.read_text())
    assert data["groups"]["group:admins"] == ["alice@example.com"]
    assert data["ssh"][0]["users"] == ["root"]
    assert data["nodeAttrs"][0]["attr"] == ["funnel"]
    assert any(g["src"] == ["tag:ci"] and g["ip"] == ["tcp:22", "tcp:3000"] for g in data["grants"])
    assert data["tagOwners"]["tag:server"] == ["autogroup:admin"]


def test_allow_all_policy_is_refused(tmp_path):
    result = _run(
        tmp_path,
        "slots: { mesh: tailscale }\nmesh: { policy: managed }\n",
        '{ "acls": [ { "action": "accept", "src": ["*"], "dst": ["*"] } ] }\n',
    )
    assert result.returncode == 1
    assert "allow" in (result.stdout + result.stderr)


def test_narrow_allow_all_rewrites_the_rule(tmp_path):
    result = _run(
        tmp_path,
        "slots: { mesh: tailscale }\nmesh: { policy: managed, narrow_allow_all: true }\n",
        '{ "acls": [ { "action": "accept", "src": ["*"], "dst": ["*"] } ] }\n',
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(OUT.read_text())
    assert data["acls"][0]["src"] == ["autogroup:member"]
    assert data["acls"][0]["dst"] == ["autogroup:member:*"]
