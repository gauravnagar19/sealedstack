"""sealedstack-enforced approval (D-052).

On GitHub Free private repos, environment required-reviewers and branch
protection are unavailable, so the write commands must enforce approval
themselves. A write command (``apply``/``up``/``restore``/``add-server``/
``server sync``) runs in CI only when **both** hold:

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

WRITE_COMMANDS = ("apply", "up", "restore", "add-server", "server sync")
APPROVE_PREFIX = "/sealedstack approve"
PLAN_MARKER = "<!-- sealedstack:plan -->"


@dataclass
class Decision:
    approved: bool
    reason: str


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
    comments = f.issue_comments(repo, number) or []
    last_plan = -1
    for index, comment in enumerate(comments):
        if PLAN_MARKER in (comment.get("body") or ""):
            last_plan = index
    for comment in comments[last_plan + 1 :]:
        if comment.get("author") in approvers and (comment.get("body") or "").strip().startswith(APPROVE_PREFIX):
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
) -> Decision:
    if not requires_approval(command):
        return Decision(True, "no approval required for this command")
    if not approvers:
        return Decision(False, "no approvers configured (set stack.yaml: approvers)")
    if not repo or not commit_sha:
        return Decision(False, "missing repository or commit sha for the approval check")

    merged, pr = _merged_through_pr(f, repo, commit_sha)
    if not merged:
        return Decision(False, "the commit is not merged through a pull request (direct pushes to main are refused)")

    if actor and actor in approvers:
        return Decision(True, f"dispatched by approver '{actor}'")

    number = pr_number or pr
    if number and approved_by_comment(f, repo, number, approvers):
        return Decision(True, "approved by an approver comment after the latest plan")

    return Decision(
        False,
        "no approval: dispatch by a listed approver, or comment '/sealedstack approve' on the PR after the latest plan",
    )


def env_request(env: Mapping[str, str]) -> dict:
    """Read the approval inputs from the CI environment."""
    return {
        "repo": env.get("GITHUB_REPOSITORY", ""),
        "commit_sha": env.get("GITHUB_SHA") or env.get("SEALEDSTACK_COMMIT", ""),
        "actor": env.get("GITHUB_ACTOR", ""),
        "pr_number": int(env["SEALEDSTACK_PR"]) if env.get("SEALEDSTACK_PR", "").isdigit() else None,
    }
