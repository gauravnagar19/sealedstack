"""Python orchestration for ``plan`` / ``apply`` / ``up`` (spec M6, D-047).

This replaces the bash case in ``scripts/sealedstack`` and the ``just`` recipe
chains with an explicit, re-runnable phase list. The heavy steps (OpenTofu,
Ansible, the renderers) are still executed as child processes, but the *order*,
guards and reporting live here so a mid-way failure can simply be re-run.

Every phase is a pure description: a name, a list of argv to run, and whether it
is only needed for ``up``. ``run_phases`` executes them in order and stops at the
first non-zero exit, returning the phase that failed.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence


@dataclass
class Phase:
    name: str
    argv: list[str]
    up_only: bool = False


def _py(code_root: Path, script: str, *args: str) -> list[str]:
    return [sys.executable or "python3", str(code_root / "scripts" / script), *args]


def _mod(module: str, *args: str) -> list[str]:
    """Run a sealedstack submodule (D-047: bash phases ported to Python)."""
    return [sys.executable or "python3", "-m", f"sealedstack.{module}", *args]


def _sh(code_root: Path, script: str, *args: str) -> list[str]:
    return [str(code_root / "scripts" / script), *args]


def phases(
    command: str,
    *,
    code_root: Path,
    stack_name: str = "",
    servers: Sequence[str] = (),
    server_ips: Optional[dict] = None,
) -> list[Phase]:
    """The ordered phases for a command.

    ``plan`` and ``apply`` share the render/preflight prefix; ``apply`` adds the
    writes and the post-apply publishing; ``up`` prepends state and interleaves
    server bootstrap between edge and platform (spec 6.2 step 6).
    """
    server_ips = server_ips or {}
    prefix = [
        Phase("render", _py(code_root, "render.py")),
        Phase("mesh-policy", _py(code_root, "mesh-policy.py")),
        Phase("build-env", _mod("buildenv")),
        Phase("user-auth", _mod("userauth")),
    ]

    if command == "plan":
        return prefix + [
            Phase("edge-plan", _mod("edge", "plan")),
            Phase("platform-plan", _mod("platform", "plan")),
        ]

    if command == "apply":
        return prefix + [
            Phase("edge-apply", _mod("edge", "apply")),
            Phase("platform-apply", _mod("platform", "apply")),
            Phase("post-apply", _mod("postapply")),
            Phase("publish", _mod("publishsettings")),
        ]

    if command == "up":
        # `generated` keeps what sealedstack creates (SSH key pairs, the Dokploy admin
        # password) in the encrypted state; it only needs the state bucket.
        result = [Phase("state-init", _mod("stateinit")), Phase("generated", _mod("generated"))]
        result += prefix
        result.append(Phase("edge-apply", _mod("edge", "apply")))
        for server in servers:
            result.append(Phase(f"server-up:{server}", _mod("server", "up", server), up_only=True))
        result += [
            # Create the Dokploy admin + API key through its API (over the tailnet).
            Phase("dokploy-bootstrap", _mod("dokploybootstrap"), up_only=True),
            Phase("platform-apply", _mod("platform", "apply")),
            Phase("post-apply", _mod("postapply")),
            Phase("publish", _mod("publishsettings")),
            Phase("status", _py(code_root, "status.py")),
        ]
        return result

    raise ValueError(f"no orchestration for {command!r}")


def _load_done(state_path: Optional[Path]) -> set:
    if state_path is None or not Path(state_path).exists():
        return set()
    try:
        return set(json.loads(Path(state_path).read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_done(state_path: Optional[Path], done: set) -> None:
    if state_path is None:
        return
    try:
        Path(state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(state_path).write_text(json.dumps(sorted(done)), encoding="utf-8")
    except OSError:
        pass


def _clear_done(state_path: Optional[Path]) -> None:
    if state_path is not None:
        try:
            Path(state_path).unlink(missing_ok=True)
        except OSError:
            pass


def run_phases(
    command: str,
    *,
    code_root: Path,
    runner: Callable[[Sequence[str], Path], int],
    stack_name: str = "",
    servers: Sequence[str] = (),
    server_ips: Optional[dict] = None,
    json_output: bool = False,
    stream=None,
    state_path: Optional[Path] = None,
) -> int:
    """Run the phases in order; stop at the first failure. Re-runnable.

    When ``state_path`` is given, completed phases are recorded there and skipped
    on a re-run, so ``up`` resumes after a mid-way failure (C3).
    """
    out = stream if stream is not None else sys.stderr
    plan = phases(
        command,
        code_root=code_root,
        stack_name=stack_name,
        servers=servers,
        server_ips=server_ips,
    )

    done = _load_done(state_path)
    records = []
    for phase in plan:
        if phase.name in done:
            print(f"==> {phase.name} (already done; skipping)", file=out)
            records.append({"phase": phase.name, "rc": 0, "skipped": True})
            continue
        print(f"==> {phase.name}", file=out)
        rc = runner(phase.argv, code_root)
        records.append({"phase": phase.name, "rc": rc})
        if rc != 0:
            _save_done(state_path, done)
            if json_output:
                print(json.dumps({"command": command, "failed": phase.name, "phases": records}))
            else:
                print(
                    f"sealedstack: '{phase.name}' failed (exit {rc}). "
                    f"Fix the cause and re-run `sealedstack {command}`; finished steps are skipped.",
                    file=out,
                )
            return rc
        done.add(phase.name)
        _save_done(state_path, done)

    _clear_done(state_path)
    if json_output:
        print(json.dumps({"command": command, "ok": True, "phases": records}))
    return 0
