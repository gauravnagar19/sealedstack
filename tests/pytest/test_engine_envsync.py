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
