"""``/sealedstack`` comment commands (M7, journey reference §12.1).

A slash-command on an issue or PR comment runs an engine command — but only for
users with **write** access to the repo (or admins); everyone else gets a polite
refusal (spec §6, user-experience §12.1). The dispatch itself is delegated to the
reusable workflow; this module only parses and authorizes.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import forge as forge_mod

PREFIX = "/sealedstack"
WRITE_LEVELS = {"admin", "write", "maintain"}

COMMANDS = {
    "setup",
    "check",
    "plan",
    "apply",
    "up",
    "add-server",
    "add-app",
    "reinstall",
    "status",
    "restore",
    "drill",
    "drift",
    "verify",
    "approve",
}


@dataclass
class Parsed:
    command: str
    args: list[str]

    @property
    def argv(self) -> list[str]:
        return [self.command, *self.args]


def parse(body: str) -> Parsed | None:
    """Parse ``/sealedstack <cmd> [args]`` (also ``/sealedstack up`` mid-line)."""
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(PREFIX):
            continue
        rest = stripped[len(PREFIX) :].strip()
        if not rest:
            return None
        parts = rest.split()
        if parts[0] not in COMMANDS:
            return None
        return Parsed(parts[0], parts[1:])
    return None


def authorize(f: forge_mod.Forge, repo: str, actor: str) -> bool:
    return f.permission(repo, actor) in WRITE_LEVELS


def handle(f: forge_mod.Forge, repo: str, number: int, body: str, actor: str) -> Parsed | None:
    """Authorize and acknowledge a comment command.

    Returns the parsed command to run (the workflow runs it), or ``None`` when
    the comment is not a command. Posts a refusal for non-writers.
    """
    parsed = parse(body)
    if parsed is None:
        return None
    if not authorize(f, repo, actor):
        f.upsert_comment(
            repo,
            number,
            f"@{actor} thanks! Only users with write access can run `{PREFIX} {parsed.command}`.",
        )
        return None
    if parsed.command == "approve":
        f.upsert_comment(repo, number, f"@{actor} approval recorded for this PR (D-052).")
        return parsed
    f.upsert_comment(repo, number, f"Running `/sealedstack {parsed.command}`…")
    return parsed
