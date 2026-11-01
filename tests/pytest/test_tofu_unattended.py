"""The first live `up` died in `state-init` with "error asking for approval: EOF": tofu apply was waiting for a typed yes in CI."""
from __future__ import annotations

from pathlib import Path

from sealedstack import config as config_mod, edge, platform, stateinit, tofu
from sealedstack.secrets import EnvSource

from test_engine_server import REPO, make_config
from test_engine_r2 import TOKEN, verify_http

CI = {"GITHUB_ACTIONS": "true"}


def test_unattended_apply_is_automatic_and_never_prompts():
    assert tofu.action_args("apply", CI) == ["-input=false", "-auto-approve"]
    assert tofu.action_args("plan", CI) == ["-input=false"]


def test_destroy_is_never_made_automatic():
    assert tofu.action_args("destroy", CI) == []


def test_a_person_at_a_terminal_keeps_the_prompt(monkeypatch):
    monkeypatch.setattr(tofu.sys.stdin, "isatty", lambda: True, raising=False)
    assert tofu.action_args("apply", {}) == []


def test_no_terminal_counts_as_unattended(monkeypatch):
    monkeypatch.setattr(tofu.sys.stdin, "isatty", lambda: False, raising=False)
    assert tofu.action_args("apply", {}) == ["-input=false", "-auto-approve"]


def test_stateinit_edge_and_platform_pass_the_flags_in_ci(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "state_passphrase": "0123456789abcdef"})
    calls = []
    assert stateinit.run(config, dict(CI), source=source, runner=lambda argv: calls.append(argv) or 0) == 0
    assert calls[-1][3] == "apply" and "-auto-approve" in calls[-1] and "-input=false" in calls[-1]
