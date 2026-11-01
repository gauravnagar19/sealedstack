"""The newest restore-drill result per app (D-085, R8/R9), kept in the state bucket.

A drill (``backup check``) ends by calling :func:`record`; the hourly ``status`` reads it back with
:func:`load` and shows one row per app in the Status issue (:func:`render_rows`). One JSON object holds
every app, at ``sealedstack/<infra name>/backup-checks.json``, next to the state lock (D-062)::

    {"apps": {"demo-app": {"app": "demo-app", "time": "2026-11-02T03:00:12Z", "level": "schema",
                           "where": "server", "outcome": "passed", ...}}}

Everything here is best effort: without R2 credentials, or when the bucket answers with an error, a drill
still reports its outcome in the job log and ``status`` still renders. Nothing here raises.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Optional

from . import lock as lock_mod

PROG = "sealedstack"
OBJECT_NAME = "backup-checks.json"
UNAVAILABLE_NO_CREDENTIALS = "no state bucket credentials"

ICON = {"passed": "✅", "failed": "❌", "skipped": "⏳"}
EMPTY_NOTE = (
    "No restore drill has run yet. Drills are off by default (`drill.scheduled` in config/stack.yaml); "
    "run `backup check <app>` by hand."
)


def record_key(infra_name: str) -> str:
    """The results object, namespaced per infra repo like ``lock.lock_key``."""
    return f"sealedstack/{infra_name or 'sealedstack'}/{OBJECT_NAME}"


def _empty() -> dict:
    return {"apps": {}}


def _client(config, env, purpose: str):
    from . import cli  # lazy: cli imports status, which imports this module

    return cli._r2_client(config, dict(env or {}), purpose=purpose)


def _warn(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else type(exc).__name__


def _read(client, key: str) -> dict:
    """The stored results; raises on a storage error, starts over on an unreadable object."""
    body = client.get(key)
    if not body:
        return _empty()
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _warn(f"{key} is not valid JSON; starting a new record of drill results")
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        return _empty()
    return data


def record(config, env, outcome: dict, *, client=None) -> bool:
    """Store ``outcome`` as the newest drill result of ``outcome["app"]``. True when written.

    Read-modify-write of the one object; a read error leaves it alone (writing then would drop the other
    apps' results), so two drills finishing in the same second can lose one row, never the file."""
    app = str((outcome or {}).get("app") or "")
    if not app:
        _warn("drill result not recorded: it names no app")
        return False
    client = client if client is not None else _client(config, env, "drill result record")
    if client is None:
        _warn(f"drill result for {app} not recorded ({UNAVAILABLE_NO_CREDENTIALS}); it is in this job's log only")
        return False
    key = record_key(getattr(config, "name", "") or "")
    try:
        data = _read(client, key)
    except Exception as exc:  # noqa: BLE001 - recording never fails the drill
        _warn(f"drill result for {app} not recorded: reading {key} failed: {_first_line(exc)}")
        return False
    data["apps"][app] = dict(outcome)
    try:
        client.put(key, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        _warn(f"drill result for {app} not recorded: writing {key} failed: {_first_line(exc)}")
        return False
    return True


def load(config, env, *, client=None) -> dict:
    """The stored results, or ``{"apps": {}}`` when there are none or they cannot be read."""
    client = client if client is not None else _client(config, env, "drill results")
    if client is None:
        return _empty()
    try:
        return _read(client, record_key(getattr(config, "name", "") or ""))
    except Exception as exc:  # noqa: BLE001
        _warn(f"drill results unavailable: {_first_line(exc)}")
        return _empty()


def load_for_status(config, env, *, client=None) -> dict:
    """Like :func:`load`, but says *why* there is nothing: ``{"apps": {}, "unavailable": "<reason>"}``.

    ``status`` must tell "no drill has run" apart from "cannot see the results"."""
    client = client if client is not None else _client(config, env, "drill results")
    if client is None:
        return {"apps": {}, "unavailable": UNAVAILABLE_NO_CREDENTIALS}
    try:
        return _read(client, record_key(getattr(config, "name", "") or ""))
    except Exception as exc:  # noqa: BLE001
        return {"apps": {}, "unavailable": f"reading the state bucket failed: {_first_line(exc)}"}


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _cell(value) -> str:
    """One markdown table cell: no pipes or line breaks from free text."""
    return " ".join(str(value or "").split()).replace("|", "\\|")


def _when(value, now: datetime) -> str:
    parsed = lock_mod._parse_time(value)
    if parsed is None:
        return _cell(value) or "—"
    return f"{lock_mod.describe_age(now - parsed)} ago"


def _backup_age(hours) -> str:
    if hours is None or hours == "":
        return "—"
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        return "—"
    if hours < 1:
        return f"{max(int(hours * 60), 0)} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.1f} d"


def _note(entry: dict) -> str:
    """The reason (failed/skipped), else the first warning (R9: a stale backup)."""
    reason = str(entry.get("reason") or "")
    if reason and entry.get("outcome") != "passed":
        return _cell(reason)
    warnings = [w for w in (entry.get("warnings") or []) if w]
    if warnings:
        return "⚠️ " + _cell(warnings[0])
    return _cell(reason)


def render_rows(data: dict, *, now: Optional[datetime] = None) -> list[str]:
    """Markdown lines for the Status issue's Backups section: the newest drill of each app (R8)."""
    data = data if isinstance(data, dict) else {}
    unavailable = str(data.get("unavailable") or "")
    if unavailable:
        return [f"drill results unavailable ({_cell(unavailable)})"]
    apps = data.get("apps") if isinstance(data.get("apps"), dict) else {}
    if not apps:
        return [EMPTY_NOTE]
    now = now or datetime.now(timezone.utc)
    lines = ["| | App | Level | Where | Last drill | Backup age | Note |", "|---|---|---|---|---|---|---|"]
    for name in sorted(apps):
        entry = apps[name] if isinstance(apps[name], dict) else {}
        lines.append(
            f"| {ICON.get(str(entry.get('outcome') or ''), '?')} | {_cell(entry.get('app') or name)} | "
            f"{_cell(entry.get('level')) or '—'} | {_cell(entry.get('where')) or '—'} | {_when(entry.get('time'), now)} | "
            f"{_backup_age(entry.get('backup_age_hours'))} | {_note(entry)} |"
        )
    return lines
