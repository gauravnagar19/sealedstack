"""The pinned Status issue (M7, journey G).

``status`` renders one issue body (servers, apps, backups, drift, advisories) and
keeps it pinned; problems are posted as **comments** so the owner gets notified.
The marker lets the updater find and edit the same issue every time.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from . import forge as forge_mod

MARKER = "<!-- sealedstack:status -->"


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
) -> str:
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

    lines += ["", "| Backups | Newest | Last restore drill |", "|---|---|---|"]
    for backup in backups or []:
        lines.append(
            f"| {backup.get('app', '')} | {backup.get('newest', '—')} | {backup.get('drill', '—')} |"
        )

    lines += ["", f"Drift: 🟢 {drift} · Advisories: 🟢 {advisories}", ""]
    return "\n".join(lines)


_ICON = {"ok": "🟢", "warn": "🟡", "fail": "🔴", "skip": "⏳"}


def render_from_health(config, version: str, health: list) -> str:
    """The Status issue body from preflight results (servers, apps, drift/advisories)."""
    by_id = {r.id: r for r in health}
    servers = []
    for name in sorted((config.servers.get("servers") or {})):
        res = by_id.get(f"servers.{name}")
        servers.append({"name": name, "mesh": f"{_ICON.get(res.status, '⏳')} {res.detail}" if res else "⏳ unknown"})
    apps = []
    for name, spec in sorted((config.apps.get("apps") or {}).items()):
        hosts = ", ".join((spec or {}).get("hostnames") or [])
        apps.append({"name": name, "url": hosts})
    failing = [r for r in health if r.status == "fail"]
    body = render(name=config.name or "infra", version=version, servers=servers, apps=apps)
    if failing:
        body = body.replace("# 🟢 ", "# 🔴 ", 1)
        body += "\n**Problems**\n" + "\n".join(f"- 🔴 `{r.id}`: {r.detail}" for r in failing) + "\n"
    return body


def issue_number(f: forge_mod.Forge, repo: str) -> Optional[int]:
    return f.get_issue(repo, MARKER)


def publish(f: forge_mod.Forge, repo: str, body: str, *, title: str = "sealedstack status") -> int:
    """Create the pinned Status issue, or update it in place."""
    number = issue_number(f, repo)
    if number is None:
        return f.create_issue(repo, title=title, body=body, pinned=True)
    f.update_issue(repo, number, title=title, body=body)
    return number


def post_problem(f: forge_mod.Forge, repo: str, text: str, *, marker: str = "") -> None:
    """Comment on the Status issue so the owner is notified.

    With a ``marker`` the same problem is edited in place instead of repeated."""
    number = issue_number(f, repo)
    if number is not None:
        f.upsert_comment(repo, number, f"{marker}\n{text}" if marker else text, marker=marker)
