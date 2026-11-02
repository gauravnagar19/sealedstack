"""``env-sync`` in Python (D-032, D-047).

Merge infra-owned env keys into each app's Dokploy compose without clobbering the
release-owned ``APP_VERSION`` or hand-set keys: comment lines, key order and
multi-line / ``\\n``-escaped values round-trip, and unknown keys are reported.
Replaces ``scripts/env-sync.sh``.

The values can contain secrets, so they are handled in-process only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import buildenv as buildenv_mod
from . import config as config_mod
from . import dokploy as dokploy_mod
from . import platformstate as platformstate_mod
from . import secrets as secrets_mod

KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


def _double_quote_closed(text: str) -> bool:
    escaped = False
    for char in text[1:]:
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return True
    return False


def _parse(text: str) -> list[tuple]:
    lines = text.split("\n") if text != "" else []
    if lines and lines[-1] == "":
        lines.pop()
    entries: list[tuple] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            entries.append(("raw", line))
            index += 1
            continue
        match = KEY_RE.match(line)
        if not match:
            entries.append(("raw", line))
            index += 1
            continue
        key, rest = match.group(1), match.group(2)
        raw = [line]
        acc = rest
        if rest.startswith('"'):
            while not _double_quote_closed(acc) and index + 1 < len(lines):
                index += 1
                raw.append(lines[index])
                acc += "\n" + lines[index]
        elif rest.startswith("'"):
            while acc.count("'") < 2 and index + 1 < len(lines):
                index += 1
                raw.append(lines[index])
                acc += "\n" + lines[index]
        entries.append(("kv", key, "\n".join(raw)))
        index += 1
    return entries


def _encode(key: str, value: str) -> str:
    if value == "":
        return key + "="
    if any(char in value for char in '\\"\n\r') or any(char.isspace() for char in value) or value.startswith("#"):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return key + '="' + escaped + '"'
    return key + "=" + value


def merge_env(current: str, infra: Mapping[str, str], label: str = "") -> tuple[str, list[str]]:
    """Return ``(merged_dotenv, unknown_keys)``."""
    entries = _parse(current)
    seen: set[str] = set()
    out: list[str] = []
    for entry in entries:
        if entry[0] == "raw":
            out.append(entry[1])
        elif entry[1] in infra:
            out.append(_encode(entry[1], str(infra[entry[1]])))
            seen.add(entry[1])
        else:
            out.append(entry[2])
    for key, value in infra.items():
        if key not in seen:
            out.append(_encode(key, str(value)))

    unknown: list[str] = []
    flagged: set[str] = set()
    for entry in entries:
        if entry[0] != "kv":
            continue
        key = entry[1]
        if key not in infra and key != "APP_VERSION" and key not in flagged:
            flagged.add(key)
            unknown.append(key)
    return "\n".join(out), unknown


class ProviderMissing(RuntimeError):
    """The app names a Dokploy secrets provider that does not exist."""


def _require_provider(client, app: str, provider: str, out) -> None:
    """Fail before writing references a deploy could not resolve (a missing provider fails every deploy)."""
    try:
        names = {str(p.get("name")) for p in client.vault_providers() if isinstance(p, dict)}
    except Exception as exc:  # noqa: BLE001 - an older Dokploy has no such router; do not block, say so
        print(f"    warn: {app}: could not list Dokploy secrets providers ({exc}); writing references anyway", file=out)
        return
    if provider not in names:
        raise ProviderMissing(
            f"{app}: Dokploy has no secrets provider named '{provider}'. In Dokploy open Settings, Secrets providers, "
            f"create one named '{provider}' (for example Doppler with the app's service token) and assign it to the "
            f"'{app}' project, then run again."
        )


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    apps: Optional[list[str]] = None,
    client: Optional[dokploy_mod.DokployClient] = None,
    state: Optional[platformstate_mod.PlatformState] = None,
    source: Optional[secrets_mod.SecretSource] = None,
    out=None,
) -> None:
    env = dict(env or {})
    out = out if out is not None else sys.stdout
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    state = state or platformstate_mod.PlatformState(config, env, source)
    client = client or dokploy_mod.from_config(config, env, source)

    apps_dir = Path(config.code_root) / ".build" / "apps"
    files = sorted(apps_dir.glob("*.json"))
    if not files:
        print("env-sync: no rendered apps; nothing to do", file=out)
        return

    app_env = state.output("app_env") or {}
    compose_ids = state.output("compose_ids") or {}

    for path in files:
        data = json.loads(path.read_text())
        app = str(data.get("name") or path.stem)
        if apps and app not in apps:
            continue
        infra = dict(app_env.get(app) or {})
        provider, references = buildenv_mod.vault_references(data)
        if references:
            _require_provider(client, app, provider, out)
            infra.update(references)  # references only, never values
        if not infra:
            print(f"==> {app}: no infra env keys; skipping", file=out)
            continue
        ids = list((compose_ids.get(app) or {}).values())
        if not ids:
            print(f"==> {app}: no compose; skipping env-sync", file=out)
            continue
        for compose_id in ids:
            current = client.compose_env(str(compose_id))
            merged, unknown = merge_env(current, infra, f"{app} ({compose_id})")
            if unknown:
                print(
                    f"    warn: {app} ({compose_id}) has unknown env keys (left untouched): "
                    f"{', '.join(unknown)}",
                    file=out,
                )
            client.compose_update(str(compose_id), merged)
            print(f"    {compose_id}: merged {len(infra)} infra key(s)", file=out)

    print("env-sync: done", file=out)
