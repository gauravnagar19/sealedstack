"""Tests for the engine config/runtime layer (spec 6.6, D-045)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import config as cfg  # noqa: E402


def git_runner(*, clean: bool = True, head: str = "abc", origin: str = "abc"):
    def run(args):
        if args == ["status", "--porcelain"]:
            return 0, ("" if clean else " M config/stack.yaml"), ""
        if args == ["rev-parse", "HEAD"]:
            return 0, head, ""
        if args == ["rev-parse", "origin/main"]:
            return 0, origin, ""
        return 0, "", ""

    return run


def test_find_code_root_uses_env():
    assert cfg.find_code_root({"SEALEDSTACK_CODE": str(REPO)}) == REPO


def test_find_code_root_walks_up_from_package():
    assert cfg.find_code_root({}) == REPO


def test_resolve_config_dir_precedence():
    assert cfg.resolve_config_dir("/x", {"SEALEDSTACK_CONFIG": "/y"}, REPO) == Path("/x")
    assert cfg.resolve_config_dir(None, {"SEALEDSTACK_CONFIG": "/y"}, REPO) == Path("/y")
    assert cfg.resolve_config_dir(None, {}, REPO) == REPO / "config"


def test_load_config_from_a_directory(tmp_path):
    (tmp_path / "stack.yaml").write_text(yaml.safe_dump({"name": "example-infra"}), encoding="utf-8")
    (tmp_path / "servers.yaml").write_text(yaml.safe_dump({"servers": {}}), encoding="utf-8")
    (tmp_path / "apps.yaml").write_text(yaml.safe_dump({"apps": {}}), encoding="utf-8")
    config = cfg.load_config(tmp_path, REPO, {})
    assert config.name == "example-infra"
    assert config.config_dir == tmp_path


def test_inspect_git_clean_and_matching():
    info = cfg.inspect_git(git_runner())
    assert info.clean is True
    assert info.matches_origin_main is True


def test_inspect_git_dirty():
    info = cfg.inspect_git(git_runner(clean=False))
    assert info.clean is False


def test_inspect_git_out_of_date():
    info = cfg.inspect_git(git_runner(head="aaa", origin="bbb"))
    assert info.matches_origin_main is False


@pytest.mark.parametrize("marker", ["GITHUB_ACTIONS", "GITLAB_CI", "SEALEDSTACK_CI"])
def test_runtime_is_ci(marker):
    runtime = cfg.Runtime(REPO, {marker: "true"}, git_runner())
    assert runtime.is_ci is True


def test_runtime_is_local_without_marker():
    assert cfg.Runtime(REPO, {}, git_runner()).is_ci is False


def test_guard_refuses_local_write_by_default():
    runtime = cfg.Runtime(REPO, {}, git_runner())
    with pytest.raises(cfg.BreakGlassError, match="refused on a local runner"):
        runtime.guard_write("apply", "example-infra")


def test_guard_allows_read_only_commands():
    runtime = cfg.Runtime(REPO, {}, git_runner())
    runtime.guard_write("plan", "example-infra")  # no raise


def test_guard_allows_writes_in_ci():
    runtime = cfg.Runtime(REPO, {"GITHUB_ACTIONS": "true"}, git_runner())
    runtime.guard_write("apply", "example-infra")  # no raise


def test_break_glass_refuses_dirty_tree(tmp_path):
    runtime = cfg.Runtime(REPO, {"SEALEDSTACK_WORKDIR": str(tmp_path)}, git_runner(clean=False))
    with pytest.raises(cfg.BreakGlassError, match="clean working tree"):
        runtime.guard_write("apply", "example-infra", break_glass=True, confirm_fn=lambda _p: "example-infra")


def test_break_glass_refuses_out_of_date_checkout(tmp_path):
    runtime = cfg.Runtime(REPO, {"SEALEDSTACK_WORKDIR": str(tmp_path)}, git_runner(head="aaa", origin="bbb"))
    with pytest.raises(cfg.BreakGlassError, match="origin/main"):
        runtime.guard_write("apply", "example-infra", break_glass=True, confirm_fn=lambda _p: "example-infra")


def test_break_glass_refuses_without_stack_name(tmp_path):
    runtime = cfg.Runtime(REPO, {"SEALEDSTACK_WORKDIR": str(tmp_path)}, git_runner())
    with pytest.raises(cfg.BreakGlassError, match="requires stack.yaml:name"):
        runtime.guard_write("apply", "", break_glass=True, confirm_fn=lambda _p: "")


def test_break_glass_refuses_wrong_confirmation(tmp_path):
    runtime = cfg.Runtime(REPO, {"SEALEDSTACK_WORKDIR": str(tmp_path)}, git_runner())
    with pytest.raises(cfg.BreakGlassError, match="did not match"):
        runtime.guard_write("apply", "example-infra", break_glass=True, confirm_fn=lambda _p: "nope")


def test_break_glass_emits_audit_line(tmp_path):
    runtime = cfg.Runtime(REPO, {"USER": "admin", "SEALEDSTACK_WORKDIR": str(tmp_path)}, git_runner())
    seen = []
    runtime.guard_write(
        "apply",
        "example-infra",
        break_glass=True,
        confirm_fn=lambda _p: "example-infra",
        audit_fn=seen.append,
    )
    assert len(seen) == 1
    assert "break-glass" in seen[0]
    assert "example-infra" in seen[0]
    assert "user=admin" in seen[0]


def test_audit_line_contains_user_time_and_commit():
    info = cfg.GitInfo(clean=True, head="deadbeef", origin_main="deadbeef")
    line = cfg.audit_line("apply", info, "example-infra", {"GITHUB_ACTOR": "bot"})
    assert "user=bot" in line
    assert "commit=deadbeef" in line
    assert "command='apply'" in line
