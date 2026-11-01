"""The pinned Status issue (M7, journey G).

``status`` renders one issue body (servers, apps, backups, drift, advisories) and
keeps it pinned; problems are posted as **comments** so the owner gets notified.
The marker lets the updater find and edit the same issue every time.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Callable, Optional

from . import drillrecord
from . import forge as forge_mod

MARKER = "<!-- sealedstack:status -->"
# The Status issue's number is remembered in this repo variable when it is created, so
# finding it again is one request instead of a search through every issue (DR-7).
NUMBER_VARIABLE = "SEALEDSTACK_STATUS_ISSUE"


def render(
    *,
    name: str,
    version: str,
    latest: str = "",
    servers: list[dict] | None = None,
    apps: list[dict] | None = None,
    backups: list[dict] | None = None,
    drift: str = "none",
    advisories: str = "none",
    updated: datetime | None = None,
    drills: dict | None = None,
) -> str:
    """``drills`` (the ``drillrecord`` data) replaces the plain ``backups`` table with the newest
    restore drill of each app (D-085, R8)."""
    when = (updated or datetime.now(timezone.utc)).strftime("%H:%M UTC")
    health = "🟢"
    version_note = f"sealedstack {version}" + (f" (latest {latest})" if latest and latest != version else " (latest)")
    lines = [MARKER, f"# {health} {name} status{' ' * 30}updated {when} · {version_note}", ""]

    lines += ["| Server | Mesh | Dokploy | Updates |", "|---|---|---|---|"]
    for server in servers or []:
        lines.append(
            f"| {server.get('name', '')} | {server.get('mesh', '🟢 online')} | "
            f"{server.get('dokploy', '🟢')} | {server.get('updates', '🟢 none pending')} |"
        )

    lines += ["", "| App | URL | Version | Last release | Health |", "|---|---|---|---|---|"]
    for app in apps or []:
        lines.append(
            f"| {app.get('name', '')} | {app.get('url', '')} | {app.get('version', '—')} | "
            f"{app.get('released', '—')} | {app.get('health', '🟢')} |"
        )

    if drills is not None:
        lines += ["", "**Backups** (newest restore drill per app)", ""]
        lines += drillrecord.render_rows(drills, now=updated)
    else:
        lines += ["", "| Backups | Newest | Last restore drill |", "|---|---|---|"]
        for backup in backups or []:
            lines.append(
                f"| {backup.get('app', '')} | {backup.get('newest', '—')} | {backup.get('drill', '—')} |"
            )

    lines += ["", f"Drift: 🟢 {drift} · Advisories: 🟢 {advisories}", ""]
    return "\n".join(lines)


_ICON = {"ok": "🟢", "warn": "🟡", "info": "◻", "fail": "🔴", "skip": "⏳"}


def render_from_health(
    config,
    version: str,
    health: list,
    *,
    drills: Optional[dict] = None,
    drills_loader: Optional[Callable] = None,
    env: Optional[dict] = None,
) -> str:
    """The Status issue body from preflight results (servers, apps, backups, drift/advisories).

    The drill results are read from the state bucket unless passed in (``drills``) or a loader is
    injected; reading them is best effort, so a missing bucket shows a note instead of failing."""
    by_id = {r.id: r for r in health}
    servers = []
    for name in sorted((config.servers.get("servers") or {})):
        res = by_id.get(f"servers.{name}")
        servers.append({"name": name, "mesh": f"{_ICON.get(res.status, '⏳')} {res.detail}" if res else "⏳ unknown"})
    apps = []
    for name, spec in sorted((config.apps.get("apps") or {}).items()):
        hosts = ", ".join((spec or {}).get("hostnames") or [])
        apps.append({"name": name, "url": hosts})
    if drills is None:
        from . import status as status_mod  # lazy: status pulls in preflight

        drills = status_mod.load_drills(config, dict(os.environ if env is None else env), drills_loader)
    failing = [f"- 🔴 `{r.id}`: {r.detail}" for r in health if r.status == "fail"]
    # R8: a failed drill is a problem until the next drill of that app passes.
    for name, entry in sorted((drills.get("apps") or {}).items()):
        if isinstance(entry, dict) and entry.get("outcome") == "failed":
            failing.append(f"- 🔴 `backup.drill.{name}`: restore drill failed ({entry.get('level', '')}): {entry.get('reason', '')}")
    body = render(name=config.name or "infra", version=version, servers=servers, apps=apps, drills=drills)
    if failing:
        body = body.replace("# 🟢 ", "# 🔴 ", 1)
        body += "\n**Problems**\n" + "\n".join(failing) + "\n"
    return body


def issue_number(f: forge_mod.Forge, repo: str) -> Optional[int]:
    return f.get_issue(repo, MARKER, variable=NUMBER_VARIABLE)


def publish(f: forge_mod.Forge, repo: str, body: str, *, title: str = "sealedstack status") -> int:
    """Create the pinned Status issue, or update it in place."""
    number = issue_number(f, repo)
    if number is None:
        return f.create_issue(repo, title=title, body=body, pinned=True, variable=NUMBER_VARIABLE)
    f.update_issue(repo, number, title=title, body=body)
    return number


def post_problem(f: forge_mod.Forge, repo: str, text: str, *, marker: str = "") -> None:
    """Comment on the Status issue so the owner is notified.

    With a ``marker`` the same problem is edited in place instead of repeated."""
    number = issue_number(f, repo)
    if number is not None:
        f.upsert_comment(repo, number, f"{marker}\n{text}" if marker else text, marker=marker)
