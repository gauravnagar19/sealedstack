"""Task 5 (m7-complete): event -> command, plan comments, the apply gate, /sealedstack
commands -- against the in-memory forge (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import approval, checklist, config as cfg, drift, forge, infraci, planreport, statusissue  # noqa: E402

SLUG = "you/my-infra"
SHA = "a" * 40


def make_config(tmp_path, approvers=("you",)):
    cdir = tmp_path / "config"
    cdir.mkdir(exist_ok=True)
    stack = {"name": "my-infra", "domain": "example.com", "approvers": list(approvers),
             "slots": {"ci": "github"}, "mesh": {"tailnet": "tail1234.ts.net"}}
    (cdir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"server-1": {"provider": "any-ssh", "role": "control"}}}))
    (cdir / "apps.yaml").write_text(yaml.safe_dump({"apps": {"hello": {"hostnames": ["hello.example.com"]}}}))
    return cfg.load_config(cdir, REPO, {})


def make_forge():
    f = forge.InMemoryForge()
    f.permissions[(SLUG, "you")] = "admin"
    f.permissions[(SLUG, "writer")] = "write"
    f.permissions[(SLUG, "reader")] = "read"
    f.branch_heads[(SLUG, "main")] = SHA
    return f


def ev(name, payload=None, *, actor="you", ref="refs/heads/main", sha=SHA, command="", args=""):
    return infraci.Event(name=name, action=(payload or {}).get("action", ""), actor=actor, repo=SLUG, ref=ref, sha=sha,
                         default_branch="main", payload=payload or {}, input_command=command, input_args=args)


def pr_event(action="opened", number=7, head_ref="feature/x", head_repo=SLUG):
    return ev("pull_request", {"action": action, "pull_request": {"number": number, "head": {"ref": head_ref, "repo": {"full_name": head_repo}}}},
              ref=f"refs/pull/{number}/merge")


def comment_event(body, *, actor, number=7, is_pr=True):
    issue = {"number": number}
    if is_pr:
        issue["pull_request"] = {"url": "x"}
    return ev("issue_comment", {"action": "created", "comment": {"body": body, "user": {"login": actor}}, "issue": issue}, actor=actor)


# ---------------------------------------------------------------- event -> command
def test_pull_request_runs_plan(tmp_path):
    for action in ("opened", "synchronize", "reopened"):
        res = infraci.resolve(pr_event(action), make_forge(), make_config(tmp_path))
        assert (res.command, res.pr, res.write) == ("plan", 7, False)
    assert not infraci.resolve(pr_event("closed"), make_forge(), make_config(tmp_path)).proceed
    assert not infraci.resolve(pr_event("labeled"), make_forge(), make_config(tmp_path)).proceed


def test_the_setup_pull_request_runs_check_instead(tmp_path):
    res = infraci.resolve(pr_event(head_ref="sealedstack/setup"), make_forge(), make_config(tmp_path))
    assert (res.command, res.pr) == ("check", 7)


def test_a_fork_pull_request_runs_nothing(tmp_path):
    res = infraci.resolve(pr_event(head_repo="mallory/my-infra"), make_forge(), make_config(tmp_path))
    assert not res.proceed and "fork" in res.note


def test_push_to_the_default_branch_applies(tmp_path):
    res = infraci.resolve(ev("push", {"commits": [{"added": [], "modified": ["config/apps.yaml"], "removed": []}]}), make_forge(), make_config(tmp_path))
    assert (res.command, res.write) == ("apply", True)


def test_push_elsewhere_or_without_config_or_without_relevant_files_runs_nothing(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    assert not infraci.resolve(ev("push", ref="refs/heads/feature"), f, c).proceed
    assert not infraci.resolve(ev("push"), f, None).proceed  # the very first commit: no config yet
    readme_only = {"commits": [{"added": [], "modified": ["README.md"], "removed": []}]}
    assert not infraci.resolve(ev("push", readme_only), f, c).proceed
    pin = {"commits": [{"added": [], "modified": ["sealedstack.version", ".github/workflows/sealedstack.yml"], "removed": []}]}
    assert infraci.resolve(ev("push", pin), f, c).command == "apply"
    assert infraci.resolve(ev("push"), f, c).command == "apply"  # no file list in the payload: apply


def test_schedule_runs_status_without_drift_when_the_switch_is_unset(tmp_path):
    # D-059: scheduled drift is off by default; status is always on
    res = infraci.resolve(ev("schedule"), make_forge(), make_config(tmp_path))
    assert (res.command, res.follow, res.write) == ("status", [], False)
    assert "SEALEDSTACK_DRIFT_ENABLED" in res.note and res.outputs()["follow"] == ""
    off = infraci.resolve(ev("schedule"), make_forge(), make_config(tmp_path), drift=False)
    assert off.follow == []


def test_schedule_runs_status_then_drift_when_the_switch_is_true(tmp_path):
    res = infraci.resolve(ev("schedule"), make_forge(), make_config(tmp_path), drift=True)
    assert (res.command, res.follow, res.write) == ("status", ["drift"], False)


def test_the_switch_is_read_from_the_environment_variable():
    assert infraci.drift_enabled({"SEALEDSTACK_DRIFT_ENABLED": "true"})
    assert infraci.drift_enabled({"SEALEDSTACK_DRIFT_ENABLED": " True "})
    for value in ("", "false", "1", "yes"):
        assert not infraci.drift_enabled({"SEALEDSTACK_DRIFT_ENABLED": value})
    assert not infraci.drift_enabled({})


def test_ci_resolve_honours_the_switch_end_to_end(tmp_path):
    import json as _json

    config = make_config(tmp_path)
    event_path = tmp_path / "event.json"
    event_path.write_text("{}")
    base = {
        "SEALEDSTACK_CONFIG": str(tmp_path / "config"), "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": SLUG, "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA, "GITHUB_ACTOR": "github-actions", "SEALEDSTACK_CODE_ROOT": str(tmp_path),
    }
    results = {}
    for label, extra in (("unset", {}), ("true", {"SEALEDSTACK_DRIFT_ENABLED": "true"})):
        out = tmp_path / f"out-{label}"
        out.write_text("")
        rc = infraci.main(["resolve", "--output", str(out)], env={**base, **extra}, forge=make_forge())
        assert rc == 0
        results[label] = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
    assert (results["unset"]["command"], results["unset"]["follow"]) == ("status", "")
    assert (results["true"]["command"], results["true"]["follow"]) == ("status", "drift")
    assert config  # the config directory was valid for both runs


def test_infra_workflow_passes_the_switch_to_the_resolve_step():
    doc = yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"]["resolve"]["steps"] if s.get("id") == "resolve")
    assert step["env"]["SEALEDSTACK_DRIFT_ENABLED"] == "${{ vars.SEALEDSTACK_DRIFT_ENABLED }}"
    assert "-e SEALEDSTACK_DRIFT_ENABLED" in step["run"]


def test_manual_drift_does_not_depend_on_the_switch(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    for drift in (False, True):
        res = infraci.resolve(ev("workflow_dispatch", actor="writer", command="drift"), f, c, drift=drift)
        assert (res.command, res.follow) == ("drift", [])
        comment = infraci.resolve(comment_event("/sealedstack drift", actor="writer"), f, c, drift=drift)
        assert comment.command == "drift"


def test_dispatch_runs_the_chosen_command_including_plan_and_apply(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    for command in ("plan", "apply", "up", "status", "check"):
        res = infraci.resolve(ev("workflow_dispatch", command=command, args="--name server-2"), f, c)
        assert (res.command, res.args) == (command, "--name server-2")
    assert not infraci.resolve(ev("workflow_dispatch"), f, c).proceed


def test_dispatch_drift_resolves_to_the_drift_command_and_needs_no_approval(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    # a writer (not an approver) can dispatch it: drift is a read command
    event = ev("workflow_dispatch", actor="writer", command="drift")
    res = infraci.resolve(event, f, c)
    assert (res.command, res.write, res.follow) == ("drift", False, [])
    assert res.needs_mesh  # it plans against the real accounts, so the runner joins the tailnet
    assert not approval.requires_approval("drift")
    calls = []
    assert infraci.execute(res, event, f, c, runner_for(calls=calls)) == 0
    assert calls == [["drift"]]  # the engine's drift command, run directly (no approval comment, no refusal)
    assert f.comments == {}


def test_drift_is_a_writers_only_comment_command(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    ok = infraci.resolve(comment_event("/sealedstack drift", actor="writer"), f, c)
    assert (ok.command, ok.write) == ("drift", False)
    refused = infraci.resolve(comment_event("/sealedstack drift", actor="reader"), f, c)
    assert not refused.proceed and "Only users with write access" in f.comments[(SLUG, 7)][0].body


def test_the_dispatch_choice_lists_in_docs_and_init_offer_drift():
    for rel in ("docs/getting-started.md", "docs/user-experience.md", "scripts/init.py"):
        text = (REPO / rel).read_text(encoding="utf-8")
        assert "options: [setup, check, plan, apply, up, add-server, add-app, status, restore, drill, drift]" in text, rel


def test_the_outputs_tell_the_workflow_what_to_do(tmp_path):
    out = infraci.resolve(ev("push"), make_forge(), make_config(tmp_path)).outputs()
    assert out["proceed"] == "true" and out["write"] == "true" and out["mesh"] == "true" and out["command"] == "apply"
    nothing = infraci.resolve(ev("push", ref="refs/heads/x"), make_forge(), make_config(tmp_path)).outputs()
    assert nothing["proceed"] == "false" and nothing["command"] == ""


# ---------------------------------------------------------------- /sealedstack comments
def test_comment_commands_for_writers(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack check", actor="writer"), f, c)
    assert (res.command, res.pr, res.checkout_ref) == ("check", 7, "refs/pull/7/merge")
    assert infraci.resolve(comment_event("/sealedstack plan", actor="writer"), f, c).command == "plan"


def test_a_non_writer_gets_a_polite_refusal_and_nothing_runs(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack check", actor="reader"), f, c)
    assert not res.proceed
    assert "Only users with write access" in f.comments[(SLUG, 7)][0].body


def test_write_commands_in_comments_need_an_approver(tmp_path):
    f, c = make_forge(), make_config(tmp_path, approvers=("you",))
    refused = infraci.resolve(comment_event("/sealedstack up", actor="writer"), f, c)
    assert not refused.proceed and "approvers" in f.comments[(SLUG, 7)][0].body
    ok = infraci.resolve(comment_event("/sealedstack up", actor="you"), f, c)
    assert (ok.command, ok.write) == ("up", True)


def test_other_comments_and_unlisted_commands_are_ignored_or_redirected(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    assert not infraci.resolve(comment_event("looks good to me", actor="you"), f, c).proceed
    assert f.comments == {}
    res = infraci.resolve(comment_event("/sealedstack restore --app x", actor="you"), f, c)
    assert not res.proceed and "Run workflow" in f.comments[(SLUG, 7)][0].body


def test_approve_by_a_non_approver_is_refused(tmp_path):
    f, c = make_forge(), make_config(tmp_path, approvers=("you",))
    res = infraci.resolve(comment_event("/sealedstack approve", actor="writer"), f, c)
    assert not res.proceed and "approvers" in f.comments[(SLUG, 7)][0].body


def test_approve_before_merge_is_recorded_and_runs_nothing(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    f.pull_requests[(SLUG, 7)] = {"number": 7, "merged": False}
    res = infraci.resolve(comment_event("/sealedstack approve", actor="you"), f, c)
    assert not res.proceed
    assert "approval recorded" in f.comments[(SLUG, 7)][0].body


def test_approve_after_merge_triggers_apply(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    f.pull_requests[(SLUG, 7)] = {"number": 7, "merged": True, "merge_commit_sha": SHA}
    res = infraci.resolve(comment_event("/sealedstack approve", actor="you"), f, c)
    assert (res.command, res.pr, res.write) == ("apply", 7, True)


def test_approve_for_an_older_merge_does_not_apply_a_newer_tip(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    f.pull_requests[(SLUG, 7)] = {"number": 7, "merged": True, "merge_commit_sha": "b" * 40}
    res = infraci.resolve(comment_event("/sealedstack approve", actor="you"), f, c)
    assert not res.proceed and "no longer the tip" in f.comments[(SLUG, 7)][0].body


# ---------------------------------------------------------------- exec: plan comment
PLAN_OUTPUT = """
  # cloudflare_zero_trust_access_application.hello will be updated in-place
  ~ resource "cloudflare_zero_trust_access_application" "hello" {}
  # cloudflare_dns_record.hello will be created
  + resource "cloudflare_dns_record" "hello" {}
Plan: 1 to add, 1 to change, 0 to destroy.
Plan: 0 to add, 0 to change, 0 to destroy.
"""


def runner_for(output="", rc=0, calls=None):
    def run(argv):
        if calls is not None:
            calls.append(list(argv))
        return rc, output

    return run


def test_the_plan_comment_is_upserted_not_duplicated(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(pr_event(), f, c)
    for _ in range(3):
        assert infraci.execute(res, pr_event(), f, c, runner_for(PLAN_OUTPUT)) == 0
    comments = f.comments[(SLUG, 7)]
    assert len(comments) == 1
    body = comments[0].body
    assert approval.PLAN_MARKER in body and "1 to create · 1 to change · 0 to destroy" in body
    assert "~ cloudflare_zero_trust_access_application.hello" in body and "+ cloudflare_dns_record.hello" in body


def test_a_failed_plan_is_reported_and_fails_the_job(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(pr_event(), f, c)
    assert infraci.execute(res, pr_event(), f, c, runner_for("Error: boom", rc=1)) == 1
    assert "The plan failed" in f.comments[(SLUG, 7)][0].body


def test_an_unchanged_plan_keeps_approvals_a_changed_plan_voids_them(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(pr_event(), f, c)
    infraci.execute(res, pr_event(), f, c, runner_for(PLAN_OUTPUT))
    f.add_comment(SLUG, 7, "you", "/sealedstack approve")
    assert approval.approved_by_comment(f, SLUG, 7, ["you"])
    infraci.execute(res, pr_event(), f, c, runner_for(PLAN_OUTPUT))  # same plan again
    assert approval.approved_by_comment(f, SLUG, 7, ["you"])
    infraci.execute(res, pr_event(), f, c, runner_for(PLAN_OUTPUT + "\nPlan: 2 to add, 0 to change, 0 to destroy.\n"))
    assert not approval.approved_by_comment(f, SLUG, 7, ["you"])
    assert len(f.comments[(SLUG, 7)]) == 2  # the plan comment (edited in place) + the approval


def test_check_on_the_setup_pr_updates_the_checklist_in_place(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    ev_ = pr_event(head_ref="sealedstack/setup")
    res = infraci.resolve(ev_, f, c)
    first = '{"results": [{"id": "config", "status": "ok", "detail": "valid"}, {"id": "secrets.cf", "status": "fail", "detail": "nope", "fix": "add it"}]}'
    second = '{"results": [{"id": "config", "status": "ok", "detail": "valid"}, {"id": "secrets.cf", "status": "ok", "detail": "present"}]}'
    calls = []
    infraci.execute(res, ev_, f, c, runner_for("warning: noise\n" + first, rc=1, calls=calls))
    infraci.execute(res, ev_, f, c, runner_for(second, rc=0))
    comments = f.comments[(SLUG, 7)]
    assert len(comments) == 1 and checklist.MARKER in comments[0].body
    assert "2 of 2 ready" in comments[0].body
    assert calls[0] == ["check", "--json"]


# ---------------------------------------------------------------- exec: the apply gate
def merged_push(f, number=7):
    f.commit_prs[(SLUG, SHA)] = [{"number": number, "merged": True}]


def test_apply_without_approval_posts_the_waiting_message_and_exits_zero(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    event = ev("push", actor="someone")
    res = infraci.resolve(event, f, c)
    calls = []
    assert infraci.execute(res, event, f, c, runner_for(calls=calls)) == 0
    assert calls == []  # nothing ran
    assert infraci.execute(res, event, f, c, runner_for(calls=calls)) == 0  # a re-run does not duplicate
    comments = f.comments[(SLUG, 7)]
    assert len(comments) == 1 and "Waiting for approval" in comments[0].body and "/sealedstack approve" in comments[0].body


def test_a_direct_push_to_main_is_refused(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    event = ev("push", actor="someone")
    res = infraci.resolve(event, f, c)
    calls = []
    assert infraci.execute(res, event, f, c, runner_for(calls=calls)) == 1
    assert calls == []


def test_approve_after_merge_end_to_end_runs_the_apply(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    f.pull_requests[(SLUG, 7)] = {"number": 7, "merged": True, "merge_commit_sha": SHA}
    # 1) the merge pushes: waiting
    push = ev("push", actor="bot")
    assert infraci.execute(infraci.resolve(push, f, c), push, f, c, runner_for()) == 0
    assert "Waiting for approval" in f.comments[(SLUG, 7)][0].body
    # 2) an approver comments /sealedstack approve -> resolves to apply
    comment = comment_event("/sealedstack approve", actor="you")
    res = infraci.resolve(comment, f, c)
    assert res.command == "apply" and res.write
    calls = []
    assert infraci.execute(res, comment, f, c, runner_for("Apply complete!", calls=calls)) == 0
    assert calls == [["apply"]]
    assert any("`apply` succeeded" in cm.body for cm in f.comments[(SLUG, 7)])


def test_an_earlier_approval_lets_the_push_apply_without_waiting(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    plan = infraci.resolve(pr_event(), f, c)
    infraci.execute(plan, pr_event(), f, c, runner_for(PLAN_OUTPUT))
    f.add_comment(SLUG, 7, "you", "/sealedstack approve")
    push = ev("push", actor="bot")
    calls = []
    assert infraci.execute(infraci.resolve(push, f, c), push, f, c, runner_for(calls=calls)) == 0
    assert calls == [["apply"]]


def test_a_dispatched_apply_by_a_non_approver_is_refused_loudly(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    event = ev("workflow_dispatch", actor="writer", command="apply")
    res = infraci.resolve(event, f, c)
    assert infraci.execute(res, event, f, c, runner_for()) == 1


def test_a_failed_apply_is_reported_on_the_pr(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    merged_push(f)
    event = ev("workflow_dispatch", actor="you", command="apply")
    res = infraci.resolve(event, f, c)
    assert infraci.execute(res, event, f, c, runner_for("Error", rc=1)) == 1
    assert "`apply` failed (exit 1)" in f.comments[(SLUG, 7)][0].body


# ---------------------------------------------------------------- status + drift
def test_status_updates_the_status_issue_and_comments_on_problems(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    event = ev("schedule", actor="github-actions")
    res = infraci.resolve(event, f, c, drift=True)

    class H:
        def __init__(self, id, status, detail=""):
            self.id, self.status, self.detail, self.fix, self.area = id, status, detail, "", "x"

    health = [H("servers.server-1", "ok", "server-1.tail1234.ts.net:22 reachable"), H("cloudflare.token", "fail", "token verify failed")]
    calls = []
    rc = infraci.execute(res, event, f, c, runner_for(calls=calls), status_fn=lambda: (c, "0.6.0", health))
    assert rc == 0
    number = statusissue.issue_number(f, SLUG)
    assert number and "my-infra status" in f.issues[number]["body"] and "hello.example.com" in f.issues[number]["body"]
    assert any("cloudflare.token" in cm.body for cm in f.comments[(SLUG, number)])
    assert calls == [["drift"]]  # the follow-up runs through the engine


def test_status_alone_runs_no_drift_follow_up_when_the_switch_is_off(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    event = ev("schedule", actor="github-actions")
    res = infraci.resolve(event, f, c)
    calls = []
    assert infraci.execute(res, event, f, c, runner_for(calls=calls), status_fn=lambda: (c, "0.6.0", [])) == 0
    assert calls == []  # status ran natively; no drift was started
    assert statusissue.issue_number(f, SLUG)


def test_drift_is_a_comment_on_the_status_issue_not_a_failure(tmp_path):
    f = make_forge()
    number = statusissue.publish(f, SLUG, statusissue.render(name="my-infra", version="0.6.0"))
    seen = []

    def runner(argv, env):
        seen.append((argv, env.get("TF_CLI_ARGS_plan", "")))
        return 2 if "edge" in " ".join(argv) and "plan" in " ".join(argv) else 0

    rc = drift.run(code_root=REPO, env={}, runner=runner, forge=f, repo=SLUG, out=open("/dev/null", "w"))
    assert rc == 0
    plans = [flag for argv, flag in seen if "plan" in " ".join(argv) and ("edge" in " ".join(argv) or "platform" in " ".join(argv))]
    assert plans and all("-detailed-exitcode" in flag for flag in plans)
    body = f.comments[(SLUG, number)][0].body
    assert "Drift detected" in body and "edge" in body and "platform" not in body


def test_no_drift_posts_nothing(tmp_path):
    f = make_forge()
    statusissue.publish(f, SLUG, statusissue.render(name="my-infra", version="0.6.0"))
    assert drift.run(code_root=REPO, env={}, runner=lambda a, e: 0, forge=f, repo=SLUG, out=open("/dev/null", "w")) == 0
    assert not f.comments


def test_drift_stops_on_a_real_error(tmp_path):
    assert drift.run(code_root=REPO, env={}, runner=lambda a, e: 1, out=open("/dev/null", "w")) == 1


# ---------------------------------------------------------------- planreport
def test_planreport_flags_stateful_destroys():
    out = "  # dokploy_postgres.example will be destroyed\nPlan: 0 to add, 0 to change, 1 to destroy.\n"
    body = planreport.render_plan(out, pr=3)
    assert "🛑" in body and "allow_destroy" in body and "1 to destroy" in body


def test_planreport_no_changes_and_ansi():
    body = planreport.render_plan("\x1b[1mNo changes.\x1b[0m Your infrastructure matches the configuration.", pr=3)
    assert "No changes." in body and "\x1b" not in body and "## sealedstack plan · PR #3" in body


def test_planreport_truncates_huge_output():
    body = planreport.render_plan("x" * 200_000, pr=1)
    assert len(body) < 60_000


# ---------------------------------------------------------------- workflow + callers
def load_wf(path):
    doc = yaml.safe_load((REPO / path).read_text(encoding="utf-8"))
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def test_infra_workflow_derives_the_command_and_runs_it_through_ci_exec():
    doc = load_wf(".github/workflows/infra.yml")
    inputs = doc["on"]["workflow_call"]["inputs"]
    assert inputs["command"]["required"] is False and inputs["command"]["default"] == ""
    assert set(doc["jobs"]) == {"resolve", "approve", "run"}
    assert doc["jobs"]["run"]["needs"] == ["resolve", "approve"]
    resolve_run = " ".join(str(s.get("run", "")) for s in doc["jobs"]["resolve"]["steps"])
    assert "ci resolve" in resolve_run
    run_text = " ".join(str(s.get("run", "")) for s in doc["jobs"]["run"]["steps"])
    assert "ci exec" in run_text
    # the run job never interpolates the dispatched command or args into the shell
    assert "inputs.command" not in run_text and "inputs.args" not in run_text
    # the approve job is only for writes
    assert "write == 'true'" in doc["jobs"]["approve"]["if"]
    # comment bodies reach the shell only through env, never inline
    assert "github.event.comment.body" not in resolve_run


def caller_choice_lists():
    lists = []
    for rel in ("docs/user-experience.md", "docs/getting-started.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "options:" in line and "setup" in line:
                lists.append((rel, line))
    sys.path.insert(0, str(REPO / "scripts"))
    import importlib.util

    spec = importlib.util.spec_from_file_location("sealedstack_init_ci", REPO / "scripts" / "init.py")
    init = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(init)
    for line in init.infra_workflow("example-owner", "0.6.0").splitlines():
        if "options:" in line:
            lists.append(("scripts/init.py", line))
    return lists


def test_the_caller_workflows_offer_plan_and_apply_and_every_trigger():
    found = caller_choice_lists()
    assert len(found) == 3
    for rel, line in found:
        for command in ("setup", "check", "plan", "apply", "up", "add-server", "add-app", "status", "restore", "drill"):
            assert command in line, (rel, command)
    for rel in ("docs/user-experience.md", "docs/getting-started.md"):
        text = (REPO / rel).read_text(encoding="utf-8")
        for trigger in ("pull_request:", "push:", "issue_comment:", "schedule:", "workflow_dispatch:"):
            assert trigger in text, (rel, trigger)
