"""Tests for the read-only secret sources (spec 11, N3)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import secrets  # noqa: E402


def test_env_source_found_and_missing():
    source = secrets.EnvSource({"FOO": "bar"})
    assert source.get("FOO").found
    assert source.get("FOO").value == "bar"
    assert source.get("NOPE").missing


def test_ci_source_is_env_backed():
    source = secrets.CiSource({"FOO": "bar"})
    assert source.kind == "ci"
    assert source.get("FOO").found


def test_bitwarden_source_caches_one_fetch():
    calls = {"secret": 0}

    def runner(argv):
        if argv[1:3] == ["project", "list"]:
            return 0, json.dumps([{"id": "p1", "name": "infra"}]), ""
        if argv[1:3] == ["secret", "list"]:
            calls["secret"] += 1
            return 0, json.dumps([{"key": "FOO", "value": "bar"}]), ""
        return 1, "", "unexpected"

    source = secrets.BitwardenSource({"BWS_ACCESS_TOKEN": "token"}, runner=runner)
    assert source.get("FOO").value == "bar"
    assert source.get("FOO").found
    assert source.get("NOPE").missing
    assert calls["secret"] == 1


def test_bitwarden_source_without_token_errors():
    source = secrets.BitwardenSource({}, runner=lambda argv: (0, "[]", ""))
    result = source.get("FOO")
    assert result.errored
    assert "BWS_ACCESS_TOKEN" in result.detail


def test_bitwarden_source_project_not_found_errors():
    source = secrets.BitwardenSource(
        {"BWS_ACCESS_TOKEN": "token"},
        runner=lambda argv: (0, json.dumps([{"id": "p1", "name": "other"}]), ""),
    )
    assert source.get("FOO").errored


def test_bitwarden_source_surfaces_runner_failure():
    source = secrets.BitwardenSource(
        {"BWS_ACCESS_TOKEN": "token"},
        runner=lambda argv: (1, "", "auth failed"),
    )
    result = source.get("FOO")
    assert result.errored
    assert "auth failed" in result.detail


def test_bitwarden_env_override_wins():
    source = secrets.BitwardenSource({"BWS_ACCESS_TOKEN": "token", "FOO": "env"}, runner=lambda argv: (1, "", ""))
    assert source.get("FOO").value == "env"


def test_sops_source_decrypts(tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "infra.enc.yaml").write_text("encrypted", encoding="utf-8")

    def runner(argv):
        return 0, yaml.safe_dump({"FOO": "bar"}), ""

    source = secrets.SopsSource(tmp_path, {}, runner=runner)
    assert source.get("FOO").value == "bar"
    assert source.get("NOPE").missing


def test_sops_source_missing_file_errors(tmp_path):
    source = secrets.SopsSource(tmp_path, {}, runner=lambda argv: (0, "", ""))
    assert source.get("FOO").errored


def test_build_source_selects_kind(tmp_path):
    assert secrets.build_source({"slots": {"secrets": "env"}}, tmp_path).kind == "env"
    assert secrets.build_source({"slots": {"secrets": "ci"}}, tmp_path).kind == "ci"
    assert secrets.build_source({"slots": {"secrets": "bitwarden"}}, tmp_path).kind == "bitwarden"
    assert secrets.build_source({"slots": {"secrets": "sops"}}, tmp_path).kind == "sops"
    assert secrets.build_source({}, tmp_path).kind == "env"
