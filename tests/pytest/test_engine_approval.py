"""D-052 approval gate (fixes round 3 §B1)."""
from __future__ import annotations

from sealedstack import approval, forge

APPROVER = "octocat"


def _forge(*, merged=True, sha="abc123", pr="42"):
    f = forge.InMemoryForge()
    if merged:
        f.commit_prs[("me/infra", sha)] = [{"number": int(pr), "merged": True}]
    return f


def test_direct_push_is_refused():
    f = forge.InMemoryForge()  # no PRs for the commit
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc", actor=APPROVER, approvers=[APPROVER])
    assert not decision.approved
    assert "not merged through a pull request" in decision.reason


def test_non_approver_dispatch_is_refused():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER])
    assert not decision.approved


def test_approver_dispatch_passes():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[APPROVER])
    assert decision.approved
    assert "approver" in decision.reason


def test_approver_comment_after_latest_plan_passes():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")
    f.add_comment("me/infra", number, APPROVER, "/sealedstack approve")
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number)
    assert decision.approved


def test_non_approver_comment_is_ignored():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")
    f.add_comment("me/infra", number, "random", "/sealedstack approve")
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number)
    assert not decision.approved


def test_approval_before_latest_plan_is_ignored():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, APPROVER, "/sealedstack approve")
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")  # newer plan
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number)
    assert not decision.approved


def test_read_command_needs_no_approval():
    f = forge.InMemoryForge()
    decision = approval.check(f, "me/infra", "plan", commit_sha="abc", actor="", approvers=[])
    assert decision.approved


def test_no_approvers_configured_refuses():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[])
    assert not decision.approved
    assert "approvers" in decision.reason
