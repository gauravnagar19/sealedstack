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
    assert added["tagOwners"] == ["tag:server", "tag:ci", "tag:release"]


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
    assert block["tagOwners"]["tag:release"] == ["autogroup:admin"]
    assert block["acls"]
    assert block["tests"]


def test_tag_ci_reaches_ssh_and_the_dokploy_api_and_nothing_else():
    """D-041: tag:ci -> tag:server on TCP 22 and 3000; the policy tests pin it."""
    m = load_mesh_policy()
    ci_acls = [r for r in m.sealedstack_acls() if r["src"] == ["tag:ci"]]
    assert len(ci_acls) == 1 and ci_acls[0]["dst"] == ["tag:server:22", "tag:server:3000"]
    ci_grants = [r for r in m.sealedstack_grants() if r["src"] == ["tag:ci"]]
    assert len(ci_grants) == 1 and ci_grants[0]["ip"] == ["tcp:22", "tcp:3000"]
    tests = m.sealedstack_tests()
    assert {"src": "tag:ci", "accept": ["tag:server:22"]} in tests
    assert {"src": "tag:ci", "accept": ["tag:server:3000"]} in tests
    assert {"src": "tag:ci", "deny": ["tag:server:5432"]} in tests
    # never the old 'CI may not SSH' assertion
    assert {"src": "tag:ci", "deny": ["tag:server:22"]} not in tests


def _reaches(rules: list[dict], form: str, src_tag: str, dst_tag: str, port: int) -> bool:
    """Tiny evaluator: does any rule let ``src_tag`` reach ``dst_tag`` on TCP ``port``?"""
    for rule in rules:
        if src_tag not in rule["src"]:
            continue
        if form == "acls":
            if rule.get("action") != "accept":
                continue
            for dst in rule["dst"]:
                host, _, dport = dst.rpartition(":")
                if host == dst_tag and dport in ("*", str(port)):
                    return True
        else:
            if dst_tag not in rule["dst"]:
                continue
            if any(ip in ("*", f"tcp:{port}") for ip in rule["ip"]):
                return True
    return False


def test_tag_release_reaches_the_dokploy_api_only(tmp_path):
    """D-057: app release jobs deploy through the Dokploy API but can never SSH or reach a database."""
    m = load_mesh_policy()
    # both policy dialects: legacy acls (the default) and grants
    for form, rules in (("acls", m.sealedstack_acls()), ("grants", m.sealedstack_grants())):
        assert _reaches(rules, form, "tag:release", "tag:server", 3000), form
        assert not _reaches(rules, form, "tag:release", "tag:server", 22), form
        assert not _reaches(rules, form, "tag:release", "tag:server", 5432), form
        # tag:ci keeps SSH and the API (D-041) and still cannot reach the database
        assert _reaches(rules, form, "tag:ci", "tag:server", 22), form
        assert _reaches(rules, form, "tag:ci", "tag:server", 3000), form
        assert not _reaches(rules, form, "tag:ci", "tag:server", 5432), form
    tests = m.sealedstack_tests()
    assert {"src": "tag:release", "accept": ["tag:server:3000"]} in tests
    assert {"src": "tag:release", "deny": ["tag:server:22", "tag:server:5432"]} in tests


def test_merged_policy_carries_tag_release_in_both_forms():
    m = load_mesh_policy()
    legacy, _ = m.merge({})
    assert legacy["tagOwners"]["tag:release"] == ["autogroup:admin"]
    assert {"action": "accept", "src": ["tag:release"], "dst": ["tag:server:3000"]} in legacy["acls"]
    modern, _ = m.merge({"grants": []})
    assert {"src": ["tag:release"], "dst": ["tag:server"], "ip": ["tcp:3000"]} in modern["grants"]
    # an owner-defined tag:release is kept (existing keys win)
    custom, added = m.merge({"tagOwners": {"tag:release": ["group:ops"]}})
    assert custom["tagOwners"]["tag:release"] == ["group:ops"]
    assert "tag:release" not in added["tagOwners"]


def test_policy_tests_have_one_string_source_each():
    """Tailscale rejects a list as a test's `src` (HTTP 400, first live `up`)."""
    for entry in load_mesh_policy().sealedstack_tests():
        assert isinstance(entry["src"], str), entry
        assert set(entry) <= {"src", "accept", "deny"}, entry
