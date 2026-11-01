"""Tests for CLI parsing, dispatch and local-run rules (spec 6.5, 6.6)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import cli  # noqa: E402
from sealedstack.http import StubClient  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

STACK = {
    "name": "example-infra",
    "domain": "example.com",
    "owner_email": "you@example.com",
    "slots": {
        "compute": "hetzner",
        "mesh": "tailscale",
        "ingress": "cloudflare",
        "gate": "cloudflare_access",
        "secrets": "bitwarden",
        "backup": "r2",
        "platform": "dokploy",
    },
    "cloudflare": {"account_id": "acct", "zone": "example.com", "team_name": "example"},
    "gate": {"policies": {"owner-only": {"emails": ["you@example.com"], "session": "24h"}}, "catch_all": True},
    "edge_security": {"bot_fight_mode": True, "block_ai_bots": True, "blocked_paths": ["/.env"]},
    "state": {"backend": "r2", "bucket": "sealedstack-state"},
    "mesh": {"policy": "unmanaged", "tailnet": "example.ts.net"},
}
SERVERS = {
    "servers": {
        "web-1": {
            "provider": "hetzner",
            "role": "control",
            "os": "ubuntu-24.04",
            "server_type": "cx22",
            "location": "fsn1",
        }
    }
}
APPS = {"apps": {}}


def make_config_dir(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "stack.yaml").write_text(yaml.safe_dump(STACK), encoding="utf-8")
    (config_dir / "servers.yaml").write_text(yaml.safe_dump(SERVERS), encoding="utf-8")
    (config_dir / "apps.yaml").write_text(yaml.safe_dump(APPS), encoding="utf-8")
    return config_dir


def clean_git(head="abc", origin="abc"):
    def runner(args):
        if args == ["status", "--porcelain"]:
            return 0, "", ""
        if args == ["rev-parse", "HEAD"]:
            return 0, head, ""
        if args == ["rev-parse", "origin/main"]:
            return 0, origin, ""
        return 0, "", ""

    return runner


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_server_add():
    ns = cli.build_parser().parse_args(["server", "add", "web-1"])
    assert cli.canonical(ns) == "server add"
    action = cli.plan_action(ns, code_root=REPO)
    assert action.argv[0][2] == "sealedstack.server"
    assert "web-1" in action.argv[0]
    # The public IP is a bootstrap secret, never a job input (D-049).
    assert not any(a.startswith("203.0.113.") for a in action.argv[0])


def test_parse_flat_alias_add_server():
    ns = cli.build_parser().parse_args(["add-server", "web-1"])
    assert cli.canonical(ns) == "server add"


def test_parse_db_verbs():
    restore = cli.build_parser().parse_args(["db", "restore", "example-app"])
    drill = cli.build_parser().parse_args(["db", "drill", "example-app"])
    assert cli.canonical(restore) == "db restore"
    assert cli.canonical(drill) == "db drill"
    assert cli.plan_action(restore, code_root=REPO).argv[0][2:] == ["sealedstack.db", "restore", "example-app"]
    assert cli.plan_action(drill, code_root=REPO).argv[0][2:] == ["sealedstack.db", "drill", "example-app"]


def test_parse_debug_providers():
    ns = cli.build_parser().parse_args(["debug", "providers"])
    action = cli.plan_action(ns, code_root=REPO)
    assert action.command == "debug providers"
    assert action.argv[0][-1] == "list"


def test_check_is_native():
    ns = cli.build_parser().parse_args(["check"])
    assert cli.plan_action(ns, code_root=REPO).native is True


def test_globals_before_and_after_noun():
    before = cli.build_parser().parse_args(["--json", "check"])
    after = cli.build_parser().parse_args(["check", "--json"])
    assert before.json is True
    assert after.json is True


def test_config_flag_is_parsed():
    ns = cli.build_parser().parse_args(["--config", "/somewhere", "check"])
    assert ns.config == "/somewhere"


def test_version_flag_exits(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--version"])
    assert "sealedstack" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# dispatch + local write rules
# --------------------------------------------------------------------------- #
def test_dispatch_plan_runs_orchestration(tmp_path):
    calls = []

    def runner(argv, code_root):
        calls.append(" ".join(list(argv)))
        return 0

    rc = cli.run(["plan"], code_root=REPO, env={}, runner=runner)
    assert rc == 0
    assert any("render.py" in c for c in calls)
    assert any("sealedstack.edge plan" in c for c in calls)


def test_local_write_refused_without_break_glass(capsys):
    calls = []
    rc = cli.run(["apply"], code_root=REPO, env={}, runner=lambda a, c: calls.append(a) or 0)
    assert rc == 2
    assert calls == []
    assert "refused on a local runner" in capsys.readouterr().err


def test_write_allowed_in_ci():
    calls = []
    rc = cli.run(
        ["apply"], code_root=REPO, env={"GITHUB_ACTIONS": "true"}, runner=lambda a, c: calls.append(" ".join(a)) or 0
    )
    assert rc == 0
    assert any("sealedstack.edge apply" in c for c in calls)


def test_break_glass_allows_write_when_clean_and_confirmed(tmp_path):
    config_dir = make_config_dir(tmp_path)
    calls = []
    audits = []
    rc = cli.run(
        ["apply", "--break-glass"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(config_dir)},
        git=clean_git(),
        runner=lambda a, c: calls.append(" ".join(a)) or 0,
        confirm_fn=lambda prompt: "example-infra",
        audit_fn=audits.append,
    )
    assert rc == 0
    assert any("sealedstack.edge apply" in c for c in calls)
    assert audits and "break-glass" in audits[0]


def test_break_glass_refuses_dirty_tree(tmp_path, capsys):
    config_dir = make_config_dir(tmp_path)
    calls = []

    def dirty_git(args):
        if args == ["status", "--porcelain"]:
            return 0, " M x", ""
        return 0, "abc", ""

    rc = cli.run(
        ["apply", "--break-glass"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(config_dir)},
        git=dirty_git,
        runner=lambda a, c: calls.append(a) or 0,
        confirm_fn=lambda prompt: "example-infra",
    )
    assert rc == 2
    assert calls == []
    assert "clean working tree" in capsys.readouterr().err


def test_break_glass_refuses_wrong_name(tmp_path, capsys):
    config_dir = make_config_dir(tmp_path)
    rc = cli.run(
        ["apply", "--break-glass"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(config_dir)},
        git=clean_git(),
        runner=lambda a, c: 0,
        confirm_fn=lambda prompt: "not-the-name",
    )
    assert rc == 2
    assert "did not match" in capsys.readouterr().err


def test_check_run_emits_json(tmp_path, capsys):
    config_dir = make_config_dir(tmp_path)
    rc = cli.run(
        ["check", "--json"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(config_dir)},
        http=StubClient(),
        secrets=EnvSource({}),
        tcp_probe=lambda host, port, timeout: True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["tool"] == "sealedstack"
    assert payload["command"] == "check"
    assert payload["engine_version"] == "0.6.16"
    assert isinstance(payload["results"], list) and payload["results"]
    assert rc in (0, 1)


def test_break_glass_refuses_when_state_lock_held(tmp_path, capsys):
    config_dir = make_config_dir(tmp_path)
    workdir = tmp_path / "wd"
    workdir.mkdir()
    (workdir / "state.lock").write_text("pid=1 command=apply\n")
    calls = []
    rc = cli.run(
        ["apply", "--break-glass"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(config_dir), "SEALEDSTACK_WORKDIR": str(workdir)},
        git=clean_git(),
        runner=lambda a, c: calls.append(a) or 0,
        confirm_fn=lambda prompt: "example-infra",
    )
    assert rc == 2
    assert calls == []  # nothing ran
    assert "state lock" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# pass-through flags (UX 12.1: `add-app --name ... --repo ...`)
# --------------------------------------------------------------------------- #
def test_split_passthrough_keeps_the_commands_own_flags_in_order():
    head, tail = cli.split_passthrough(["app", "add", "--name", "x", "--repo", "a/b", "--host", "x.example.com"])
    assert head == ["app", "add"] and tail == ["--name", "x", "--repo", "a/b", "--host", "x.example.com"]
    head, tail = cli.split_passthrough(["--json", "add-app", "--name", "x"])
    assert head == ["--json", "add-app"] and tail == ["--name", "x"]
    head, tail = cli.split_passthrough(["restore", "shop", "--backup", "k", "--force"])
    assert head == ["restore", "shop"] and tail == ["--backup", "k", "--force"]
    head, tail = cli.split_passthrough(["db", "drill", "shop", "orders"])
    assert head == ["db", "drill", "shop"] and tail == ["orders"]
    # commands that do not take their own flags are left to argparse
    assert cli.split_passthrough(["check", "--json"]) == (["check", "--json"], [])
    assert cli.split_passthrough(["server", "add", "web-1"]) == (["server", "add", "web-1"], [])


def test_add_app_flags_reach_the_command(tmp_path):
    config_dir = make_config_dir(tmp_path)
    calls = []

    def runner(argv, cwd):
        calls.append(list(argv))
        return 0

    rc = cli.run(
        ["--config", str(config_dir), "add-app", "--name", "example-app", "--repo", "you/example-app", "--host", "app.example.com", "--access", "owner-only"],
        code_root=REPO,
        env={},
        git=clean_git(),
        runner=runner,
    )
    assert rc == 0
    assert calls == [[
        sys.executable, "-m", "sealedstack.appadd",
        "--name", "example-app", "--repo", "you/example-app", "--host", "app.example.com", "--access", "owner-only",
    ]]
