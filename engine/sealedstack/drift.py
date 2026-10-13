"""Drift detection (UX §10): a read-only plan that reports out-of-band changes.

Runs the same prefix as ``plan`` (render, policy merge, build env) and then each
stack's plan with ``-detailed-exitcode``: exit 2 means the real world differs
from git. Drift is not a job failure; it is posted as a comment on the Status
issue so the owner is notified, with the cause and the fix (UX §10).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import orchestrate
from . import statusissue

DRIFT_ARGS = "-detailed-exitcode -no-color"

Runner = Callable[[Sequence[str], Mapping[str, str]], int]


def _default_runner(argv: Sequence[str], env: Mapping[str, str]) -> int:
    return subprocess.run(list(argv), env=dict(env)).returncode


def message(stacks: Sequence[str]) -> str:
    return (
        f"🟡 **Drift detected** in: {', '.join(stacks)}. Something differs from what git says.\n"
        "Run `apply` to restore the configured state, or adopt the change in a pull request."
    )


def run(
    *,
    code_root: Path,
    env: Mapping[str, str],
    runner: Optional[Runner] = None,
    forge=None,
    repo: str = "",
    out=None,
) -> int:
    """Return 0 when the plans ran (drift or not), the failing exit code otherwise."""
    out = out if out is not None else sys.stderr
    runner = runner or _default_runner
    drifted: list[str] = []
    for phase in orchestrate.phases("plan", code_root=Path(code_root)):
        child_env = dict(env)
        is_plan = phase.name.endswith("-plan")
        if is_plan:
            child_env["TF_CLI_ARGS_plan"] = DRIFT_ARGS
        print(f"==> drift: {phase.name}", file=out)
        rc = runner(phase.argv, child_env)
        if is_plan and rc == 2:
            drifted.append(phase.name[: -len("-plan")])
        elif rc != 0:
            print(f"sealedstack: drift detection failed at '{phase.name}' (exit {rc})", file=out)
            return rc
    if not drifted:
        print("drift: none", file=out)
        return 0
    text = message(drifted)
    print(text, file=out)
    if forge is not None and repo:
        statusissue.post_problem(forge, repo, text)
    return 0
