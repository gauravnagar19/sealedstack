"""Unit tests for the Python helper in scripts/mesh-policy.py.

The file name contains a hyphen, so it is loaded from its path instead of being
imported by name.
"""
import importlib.util
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]

# mesh-policy.py imports its sibling configdir, which is only importable when
# scripts/ is on sys.path.
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))


def load_mesh_policy():
    path = REPO / "scripts" / "mesh-policy.py"
    spec = importlib.util.spec_from_file_location("mesh_policy", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_merge_appends_rules_and_keeps_passthrough():
    m = load_mesh_policy()
    policy = {
        "groups": {"group:admins": ["alice@example.com"]},
        "ssh": [{"action": "accept", "users": ["root"]}],
        "grants": [{"src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:3000"]}],
    }
    merged, added = m.merge(policy)
    assert merged["groups"] == {"group:admins": ["alice@example.com"]}
    assert merged["ssh"] == [{"action": "accept", "users": ["root"]}]
    assert merged["tagOwners"]["tag:server"] == ["autogroup:admin"]
    assert any(rule.get("src") == ["tag:ci"] for rule in merged["grants"])
    assert added["tagOwners"] == ["tag:server", "tag:ci"]


def test_merge_does_not_overwrite_existing_tag_owners():
    m = load_mesh_policy()
    policy = {"tagOwners": {"tag:server": ["group:ops"]}}
    merged, added = m.merge(policy)
    assert merged["tagOwners"]["tag:server"] == ["group:ops"]
    assert "tag:server" not in added["tagOwners"]


def test_broad_rules_detects_allow_all():
    m = load_mesh_policy()
    policy = {"acls": [{"action": "accept", "src": ["*"], "dst": ["*"]}]}
    broad = m.broad_rules(policy)
    assert len(broad) == 1
    assert m.is_allow_all(broad[0][1])


def test_narrow_rewrites_only_the_allow_all_rule():
    m = load_mesh_policy()
    other = {"action": "accept", "src": ["group:admins"], "dst": ["tag:server"]}
    policy = {
        "acls": [
            {"action": "accept", "src": ["*"], "dst": ["*"]},
            other,
        ]
    }
    rewrites = m.narrow_broad_rules(policy)
    assert len(rewrites) == 1
    assert policy["acls"][0]["src"] == ["autogroup:member"]
    assert policy["acls"][0]["dst"] == ["autogroup:member:*"]
    assert policy["acls"][1] is other
    assert m.broad_rules(policy) == []


def test_sealedstack_block_is_complete():
    m = load_mesh_policy()
    block = m.sealedstack_block()
    assert block["tagOwners"]["tag:server"] == ["autogroup:admin"]
    assert block["tagOwners"]["tag:ci"] == ["autogroup:admin"]
    assert block["acls"]
    assert block["tests"]
