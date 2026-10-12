"""D-052 approval gate (fixes round 3 §B1)."""
from __future__ import annotations

from sealedstack import approval, forge

APPROVER = "octocat"
MAIN = "refs/heads/main"


def _forge(*, merged=True, sha="abc123", pr="42"):
    f = forge.InMemoryForge()
    f.branch_heads[("me/infra", "main")] = sha
    if merged:
        f.commit_prs[("me/infra", sha)] = [{"number": int(pr), "merged": True}]
    return f


def test_direct_push_is_refused():
    f = forge.InMemoryForge()  # no PRs for the commit
    f.branch_heads[("me/infra", "main")] = "abc"
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc", actor=APPROVER, approvers=[APPROVER], ref=MAIN)
    assert not decision.approved
    assert "not merged through a pull request" in decision.reason


def test_non_approver_dispatch_is_refused():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], ref=MAIN)
    assert not decision.approved


def test_approver_dispatch_passes():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[APPROVER], ref=MAIN)
    assert decision.approved
    assert "approver" in decision.reason


def test_approver_comment_after_latest_plan_passes():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")
    f.add_comment("me/infra", number, APPROVER, "/sealedstack approve")
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number, ref=MAIN)
    assert decision.approved


def test_non_approver_comment_is_ignored():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")
    f.add_comment("me/infra", number, "random", "/sealedstack approve")
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number, ref=MAIN)
    assert not decision.approved


def test_approval_before_latest_plan_is_ignored():
    f = _forge()
    number = 42
    f.add_comment("me/infra", number, APPROVER, "/sealedstack approve")
    f.add_comment("me/infra", number, "github-actions", f"plan\n{approval.PLAN_MARKER}")  # newer plan
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor="random", approvers=[APPROVER], pr_number=number, ref=MAIN)
    assert not decision.approved


def test_read_command_needs_no_approval():
    f = forge.InMemoryForge()
    decision = approval.check(f, "me/infra", "plan", commit_sha="abc", actor="", approvers=[], ref=MAIN)
    assert decision.approved


def test_no_approvers_configured_refuses():
    f = _forge()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[], ref=MAIN)
    assert not decision.approved
    assert "approvers" in decision.reason


def test_feature_branch_dispatch_is_refused_even_at_a_merged_sha():
    """R3-2: a dispatch on another branch must not apply, even if its head was merged."""
    f = _forge()
    decision = approval.check(
        f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[APPROVER],
        ref="refs/heads/feat/old",
    )
    assert not decision.approved
    assert "refs/heads/main" in decision.reason


def test_stale_main_commit_is_refused():
    """R3-2: a commit that is no longer the head of main is refused."""
    f = _forge(sha="abc123")
    f.branch_heads[("me/infra", "main")] = "def456"
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[APPROVER], ref=MAIN)
    assert not decision.approved
    assert "not the current head" in decision.reason


def test_unknown_head_is_refused():
    f = _forge()
    f.branch_heads.clear()
    decision = approval.check(f, "me/infra", "apply", commit_sha="abc123", actor=APPROVER, approvers=[APPROVER], ref=MAIN)
    assert not decision.approved


def test_env_request_reads_ref_and_branch():
    req = approval.env_request({"GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "x", "GITHUB_REPOSITORY": "me/infra"})
    assert req["ref"] == "refs/heads/main" and req["branch"] == "main"
    req = approval.env_request({"SEALEDSTACK_DEFAULT_BRANCH": "trunk"})
    assert req["branch"] == "trunk"
