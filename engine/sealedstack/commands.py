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
    "backup",
    "restore",
    "drill",
    "drift",
    "verify",
    "approve",
}

# D-085: the `backup` noun's verbs -> the one command name CI resolves, gates and runs.
# `restore`/`drill` stay the canonical names (the approval gate, the workflow's docker
# socket and the weekly cron key on them); `backup list` is a read with no older alias.
BACKUP_VERBS = {"check": "drill", "restore": "restore", "list": "backup list"}


def canonical(command: str, args: str) -> tuple[str, str]:
    """The canonical ``(command, args)`` for a comment or dispatched command (D-085).

    ``backup check demo-app`` -> ``("drill", "demo-app")``, ``backup restore demo-app`` ->
    ``("restore", "demo-app")``, ``backup list`` -> ``("backup list", "")``. Anything else is
    returned unchanged, including ``backup`` without a known verb (callers refuse it).
    """
    if command != "backup":
        return command, args
    verb, _, rest = (args or "").strip().partition(" ")
    if verb not in BACKUP_VERBS:
        return command, args
    return BACKUP_VERBS[verb], rest.strip()


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
