"""D-085: the `backup` noun (list, check, restore) on the CLI and in CI, and approver-only drills (R7)."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import approval, cli, commands, config as cfg, infraci  # noqa: E402
from test_infra_ci import SLUG, comment_event, ev, make_config, make_forge, merged_push, runner_for  # noqa: E402


def db_argv(argv, env=None):
    """Run the CLI with a recording runner; return (rc, the `-m sealedstack.db ...` argv tails)."""
    calls = []
    rc = cli.run(argv, code_root=REPO, env=env or {}, runner=lambda a, c: calls.append(list(a)) or 0)
    return rc, [call[1:] for call in calls]


# ---------------------------------------------------------------- CLI
def test_the_three_backup_verbs_parse_to_their_db_actions():
    parser = cli.build_parser()
    listing = parser.parse_args(["backup", "list"])
    check = parser.parse_args(["backup", "check", "demo-app"])
    restore = parser.parse_args(["backup", "restore", "demo-app"])
    assert [cli.canonical(ns) for ns in (listing, check, restore)] == ["backup list", "backup check", "backup restore"]
    assert cli.plan_action(listing, code_root=REPO).argv[0][1:] == ["-m", "sealedstack.db", "list"]
    assert cli.plan_action(check, code_root=REPO).argv[0][1:] == ["-m", "sealedstack.db", "drill", "demo-app"]
    assert cli.plan_action(restore, code_root=REPO).argv[0][1:] == ["-m", "sealedstack.db", "restore", "demo-app"]


def test_backup_check_passes_the_table_level_and_place_through_untouched():
    rc, calls = db_argv(["backup", "check", "demo-app", "users", "--level", "sample", "--where", "runner"])
    assert rc == 0 and calls == [["-m", "sealedstack.db", "drill", "demo-app", "users", "--level", "sample", "--where", "runner"]]
    rc, calls = db_argv(["backup", "check", "all", "--level=verify"])
    assert calls == [["-m", "sealedstack.db", "drill", "all", "--level=verify"]]
    rc, calls = db_argv(["backup", "list", "--json"])
    assert calls == [["-m", "sealedstack.db", "list", "--json"]]


def test_the_old_drill_and_restore_spellings_still_work():
    assert db_argv(["drill", "demo-app", "--where", "server"])[1] == [["-m", "sealedstack.db", "drill", "demo-app", "--where", "server"]]
    assert db_argv(["db", "drill", "demo-app", "--level", "full"])[1] == [["-m", "sealedstack.db", "drill", "demo-app", "--level", "full"]]
    ci = {"GITHUB_ACTIONS": "true"}
    assert db_argv(["restore", "demo-app", "--force"], ci)[1] == [["-m", "sealedstack.db", "restore", "demo-app", "--force"]]
    assert db_argv(["db", "restore", "demo-app", "k1"], ci)[1] == [["-m", "sealedstack.db", "restore", "demo-app", "k1"]]


def test_backup_restore_is_a_guarded_write_like_restore(capsys):
    rc, calls = db_argv(["backup", "restore", "demo-app", "--force"])
    assert rc == 2 and calls == []
    assert "refused on a local runner" in capsys.readouterr().err
    rc, calls = db_argv(["backup", "restore", "demo-app", "nightly/2026-11-01.dump", "--force"], {"GITHUB_ACTIONS": "true"})
    assert rc == 0 and calls == [["-m", "sealedstack.db", "restore", "demo-app", "nightly/2026-11-01.dump", "--force"]]


def test_write_classification():
    assert "backup restore" in cfg.WRITE_COMMANDS and "db restore" in cfg.WRITE_COMMANDS
    assert "backup check" not in cfg.WRITE_COMMANDS and "backup list" not in cfg.WRITE_COMMANDS
    assert approval.requires_approval("backup restore") and approval.requires_approval("restore")
    for read in ("drill", "backup check", "backup list"):
        assert not approval.requires_approval(read), read


def test_the_help_shows_the_backup_commands_and_the_aliases():
    text = cli.build_parser().format_help()
    for line in ("backup list", "backup check <app|all>", "backup restore <app>", "are aliases (D-085)"):
        assert line in text, line


# ---------------------------------------------------------------- canonical names
def test_canonical_maps_the_backup_verbs():
    assert commands.canonical("backup", "check demo-app --level sample") == ("drill", "demo-app --level sample")
    assert commands.canonical("backup", "  restore   demo-app --force") == ("restore", "demo-app --force")
    assert commands.canonical("backup", "list") == ("backup list", "")
    # anything else is unchanged (and `backup` without a known verb is left for the caller to refuse)
    assert commands.canonical("drill", "all") == ("drill", "all")
    assert commands.canonical("plan", "") == ("plan", "")
    assert commands.canonical("backup", "") == ("backup", "")
    assert commands.canonical("backup", "nuke demo-app") == ("backup", "nuke demo-app")
    assert "backup" in commands.COMMANDS and "backup" in infraci.DISPATCH_COMMANDS


# ---------------------------------------------------------------- workflow_dispatch
def dispatch(f, c, args, actor="you", command="backup"):
    return infraci.resolve(ev("workflow_dispatch", actor=actor, command=command, args=args), f, c)


def test_a_dispatched_backup_resolves_to_the_canonical_command(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    check = dispatch(f, c, "check demo-app --where runner")
    assert (check.command, check.args, check.write) == ("drill", "demo-app --where runner", False)
    restore = dispatch(f, c, "restore demo-app")
    assert (restore.command, restore.args, restore.write) == ("restore", "demo-app", True)
    assert restore.outputs()["write"] == "true"
    listing = dispatch(f, c, "list", actor="writer")  # a read: open to anyone who can dispatch
    assert (listing.command, listing.args, listing.write) == ("backup list", "", False)
    for bad in ("", "nuke demo-app", "drill demo-app"):
        res = dispatch(f, c, bad)
        assert not res.proceed and "backup needs a verb" in res.note, bad


def test_a_dispatched_backup_restore_goes_through_the_approval_gate(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    event = ev("workflow_dispatch", actor="writer", command="backup", args="restore demo-app")
    calls = []
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(calls=calls)) == 1
    assert calls == []  # refused: not an approver, nothing ran
    event = ev("workflow_dispatch", actor="you", command="backup", args="restore demo-app")
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(calls=calls)) == 0
    assert calls == [["restore", "demo-app"]]


def test_a_dispatched_backup_list_runs_the_backup_list_command(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    event = ev("workflow_dispatch", actor="writer", command="backup", args="list")
    calls = []
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(calls=calls)) == 0
    assert calls == [["backup", "list"]]


def test_only_an_approver_may_dispatch_a_drill(tmp_path):
    f, c = make_forge(), make_config(tmp_path, approvers=("you",))
    for command, args in (("drill", "demo-app"), ("backup", "check demo-app")):
        refused = dispatch(f, c, args, actor="writer", command=command)
        assert not refused.proceed and "only the approvers" in refused.note and "writer" in refused.note
        ok = dispatch(f, c, args, actor="you", command=command)
        assert (ok.command, ok.args) == ("drill", "demo-app")


def test_the_scheduled_weekly_drill_needs_no_approver(tmp_path):
    f, c = make_forge(), make_config(tmp_path, approvers=("you",))
    c.stack["drill"] = {"scheduled": True}
    event = ev("schedule", actor="github-actions[bot]")
    event.payload = {"schedule": infraci.DRILL_CRON}
    assert infraci.resolve(event, f, c).command == "drill"


# ---------------------------------------------------------------- /sealedstack comments
def test_comment_backup_check_by_an_approver_runs_a_drill(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack backup check demo-app --level sample", actor="you"), f, c)
    assert (res.command, res.args, res.write, res.checkout_ref) == ("drill", "demo-app --level sample", False, "")


def test_comment_drills_are_refused_for_writers_who_are_not_approvers(tmp_path):
    f, c = make_forge(), make_config(tmp_path, approvers=("you",))
    for body in ("/sealedstack backup check demo-app", "/sealedstack drill demo-app"):
        res = infraci.resolve(comment_event(body, actor="writer"), f, c)
        assert not res.proceed
    bodies = [cm.body for cm in f.comments[(SLUG, 7)]]
    assert "`/sealedstack backup check`" in bodies[0] and "`/sealedstack drill`" in bodies[1]
    assert all("only the approvers listed in `stack.yaml: approvers`" in b for b in bodies)


def test_comment_backup_list_is_a_read_for_any_writer(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack backup list", actor="writer", number=3, is_pr=False), f, c)
    assert (res.command, res.args, res.write) == ("backup list", "", False)
    refused = infraci.resolve(comment_event("/sealedstack backup list", actor="reader"), f, c)
    assert not refused.proceed and "Only users with write access can run `/sealedstack backup list`" in f.comments[(SLUG, 7)][0].body


def test_comment_backup_restore_is_redirected_like_restore(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack backup restore demo-app", actor="you"), f, c)
    assert not res.proceed
    assert "`/sealedstack backup restore` can only be started from Actions, Run workflow" in f.comments[(SLUG, 7)][0].body


def test_comment_backup_without_a_verb_is_refused_and_free_text_is_not_echoed(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack backup @everyone`x`", actor="you"), f, c)
    assert not res.proceed
    body = f.comments[(SLUG, 7)][0].body
    assert "needs a verb" in body and "@everyone" not in body


# ---------------------------------------------------------------- infra.yml: docker socket only for drills
def exec_steps():
    data = yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))
    return [s for s in data["jobs"]["run"]["steps"] if "ci exec" in s.get("run", "")]


def docker_args(tmp_path, script: str, command: str) -> list:
    """Run an exec step's script with a fake `docker` on PATH; return the args it was given."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$DOCKER_ARGS"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (work / "sealedstack.version").write_text("0.9.0\n")
    out = tmp_path / f"args-{command}"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "DOCKER_ARGS": str(out), "GITHUB_EVENT_PATH": "/dev/null",
           "SEALEDSTACK_RESOLVED_COMMAND": command}
    script = script.replace("${{ github.repository_owner }}", "owner")
    subprocess.run(["bash", "-c", script], cwd=work, env=env, check=True)
    return out.read_text().splitlines()


def test_only_a_drill_gets_the_runner_docker_socket(tmp_path):
    steps = exec_steps()
    assert len(steps) == 2  # both secret-source variants of the run step
    for step in steps:
        drill = docker_args(tmp_path, step["run"], "drill")
        sock = drill.index("/var/run/docker.sock:/var/run/docker.sock")
        assert drill[sock - 1] == "-v" and drill[-2:] == ["ci", "exec"]
        without_sock = drill[: sock - 1] + drill[sock + 1 :]
        for other in ("plan", "restore", "backup list", "apply", ""):
            args = docker_args(tmp_path, step["run"], other)
            assert args == without_sock, other  # every other command keeps exactly its flags
