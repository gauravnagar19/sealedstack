"""sealedstack-enforced approval (D-052).

On GitHub Free private repos, environment required-reviewers and branch
protection are unavailable, so the write commands must enforce approval
themselves. A write command (``apply``/``up``/``restore``/``add-server``/
``server sync``/``reinstall``) runs in CI only when **both** hold:

1. the commit is on ``main`` **and** the forge reports it merged through a PR
   (a direct push to ``main`` is refused); and
2. it is approved: the run was dispatched by a listed approver, **or** a listed
   approver commented ``/sealedstack approve`` on that PR **after** the latest
   plan comment.

GitHub environments with required reviewers are still used where the plan
supports them (defence in depth), never as the only gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from . import forge as forge_mod

# `backup restore` is `restore` under the D-085 noun; CI resolves it to `restore` first,
# but the gate holds for either spelling.
WRITE_COMMANDS = ("apply", "up", "restore", "backup restore", "add-server", "server sync", "reinstall")
APPROVE_PREFIX = "/sealedstack approve"
PLAN_MARKER = "<!-- sealedstack:plan -->"


@dataclass
class Decision:
    approved: bool
    reason: str
    # True only when everything else holds (merged through a PR, on main's tip)
    # and the sole thing missing is an approver's `/sealedstack approve`.
    waiting: bool = False
    pr: Optional[int] = None


def requires_approval(command: str) -> bool:
    return command in WRITE_COMMANDS


def _merged_through_pr(f: forge_mod.Forge, repo: str, sha: str) -> tuple[bool, Optional[int]]:
    prs = f.pull_requests_for_commit(repo, sha) or []
    for pr in prs:
        if pr.get("merged"):
            return True, int(pr["number"]) if pr.get("number") else None
    number = int(prs[0]["number"]) if prs and prs[0].get("number") else None
    return False, number


def approved_by_comment(f: forge_mod.Forge, repo: str, number: int, approvers: Sequence[str]) -> bool:
    """An approver's ``/sealedstack approve`` newer than the latest plan comment.

    The plan comment is edited in place when the plan changes, so "after" means
    after its last edit (``updated``), not after its position in the thread; an
    approval given for an older plan therefore no longer counts. Forges that give
    no timestamps fall back to thread order.
    """
    comments = f.issue_comments(repo, number) or []
    last_plan = -1
    for index, comment in enumerate(comments):
        if PLAN_MARKER in (comment.get("body") or ""):
            last_plan = index
    plan_time = None
    if last_plan >= 0:
        plan = comments[last_plan]
        plan_time = plan.get("updated") or plan.get("created") or None
    for index, comment in enumerate(comments):
        if comment.get("author") not in approvers:
            continue
        if not (comment.get("body") or "").strip().startswith(APPROVE_PREFIX):
            continue
        created = comment.get("created")
        if plan_time is not None and created not in (None, ""):
            if created > plan_time:
                return True
        elif index > last_plan:
            return True
    return False


def check(
    f: forge_mod.Forge,
    repo: str,
    command: str,
    *,
    commit_sha: str = "",
    actor: str = "",
    approvers: Sequence[str] = (),
    pr_number: Optional[int] = None,
    ref: str = "",
    branch: str = "main",
) -> Decision:
    if not requires_approval(command):
        return Decision(True, "no approval required for this command")
    if not approvers:
        return Decision(False, "no approvers configured (set stack.yaml: approvers)")
    if not repo or not commit_sha:
        return Decision(False, "missing repository or commit sha for the approval check")

    # Writes run only from the tip of the protected branch (review R3-2): a
    # dispatch on another branch, even one whose head is a merged PR commit,
    # would apply a stale tree.
    expected_ref = f"refs/heads/{branch}"
    if ref != expected_ref:
        return Decision(False, f"write commands run only on {expected_ref} (this run is on '{ref or 'unknown'}')")
    head = f.branch_head(repo, branch)
    if not head:
        return Decision(False, f"could not read the head of {branch}; refusing")
    if head != commit_sha:
        return Decision(False, f"the commit is not the current head of {branch} ({commit_sha[:7]} != {head[:7]}); re-run on the latest {branch}")

    merged, pr = _merged_through_pr(f, repo, commit_sha)
    if not merged:
        return Decision(False, "the commit is not merged through a pull request (direct pushes to main are refused)", pr=pr)

    if actor and actor in approvers:
        return Decision(True, f"dispatched by approver '{actor}'", pr=pr)

    number = pr_number or pr
    if number and approved_by_comment(f, repo, number, approvers):
        return Decision(True, "approved by an approver comment after the latest plan", pr=number)

    return Decision(
        False,
        "no approval: dispatch by a listed approver, or comment '/sealedstack approve' on the PR after the latest plan",
        waiting=True,
        pr=number,
    )


def env_request(env: Mapping[str, str]) -> dict:
    """Read the approval inputs from the CI environment."""
    return {
        "repo": env.get("GITHUB_REPOSITORY", ""),
        "commit_sha": env.get("GITHUB_SHA") or env.get("SEALEDSTACK_COMMIT", ""),
        "actor": env.get("GITHUB_ACTOR", ""),
        "pr_number": int(env["SEALEDSTACK_PR"]) if env.get("SEALEDSTACK_PR", "").isdigit() else None,
        "ref": env.get("GITHUB_REF", "") or env.get("SEALEDSTACK_REF", ""),
        "branch": env.get("SEALEDSTACK_DEFAULT_BRANCH", "") or "main",
    }
