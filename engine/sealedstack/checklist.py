"""The Setup PR living checklist (M7, journey A step 4).

The engine turns preflight results into one comment that updates in place on the
Setup PR. The marker line is what ``upsert_comment`` matches on, so ``/sealedstack
check`` rewrites the same comment instead of posting a new one.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

MARKER = "<!-- sealedstack:setup-checklist -->"

_ICON = {"ok": "✅", "fail": "❌", "pend": "⏳", "skip": "—", "warn": "❌", "info": "◻"}


@dataclass
class Item:
    status: str  # ok | fail | pend | skip | warn | info (recommended, non-blocking)
    label: str
    detail: str = ""
    fix: str = ""


def render(items: Sequence[Item], *, updated: datetime | None = None) -> str:
    when = (updated or datetime.now(timezone.utc)).strftime("%H:%M UTC")
    ready = sum(1 for item in items if item.status == "ok")
    total = sum(1 for item in items if item.status not in ("skip", "info"))
    lines = [
        MARKER,
        f"## sealedstack setup checklist{' ' * 35}(updated {when})",
        "",
        f"Progress: {ready} of {total} ready. Fix the ❌ items below, then comment `/sealedstack check`.",
        "",
        "| | Prerequisite | Status |",
        "|---|---|---|",
    ]
    for item in items:
        label = f"**{item.label}**" if item.status in ("fail", "warn") else item.label
        detail = item.detail
        if item.fix:
            detail = f"{detail} → {item.fix}" if detail else item.fix
        lines.append(f"| {_ICON.get(item.status, '—')} | {label} | {detail} |")
    if any(item.status == "info" for item in items):
        lines += ["", "◻ rows are recommendations: they do not block the merge and are not counted above."]
    lines += ["", "Merge this PR when everything above is ✅. Nothing has been changed outside this repo yet."]
    return "\n".join(lines)


def from_results(results: Iterable) -> list[Item]:
    """Adapt preflight ``Result`` objects (status ok|warn|info|fail|skip) to items."""
    mapping = {"ok": "ok", "warn": "warn", "info": "info", "fail": "fail", "skip": "skip"}
    items: list[Item] = []
    for result in results:
        items.append(
            Item(
                status=mapping.get(getattr(result, "status", "fail"), "fail"),
                label=getattr(result, "id", ""),
                detail=getattr(result, "detail", ""),
                fix=getattr(result, "fix", ""),
            )
        )
    return items
