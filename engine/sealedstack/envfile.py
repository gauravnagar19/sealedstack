"""``.build/env.json`` — the per-app env map the platform stack injects.

Both ``build-env`` and ``user-auth`` write into this one file, so the load /
merge / atomic-save logic lives here (D-047: bash → Python).

The file maps ``app -> {ENV_NAME: value}``. Values may be secrets, so the file
is written atomically and never logged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping


class EnvError(RuntimeError):
    """A required app secret could not be resolved."""


def env_path(code_root: Path) -> Path:
    return Path(code_root) / ".build" / "env.json"


def load(path: Path) -> dict:
    if not Path(path).exists():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
    return data if isinstance(data, dict) else {}


def save(path: Path, data: Mapping[str, dict]) -> None:
    """Write atomically: a half-written env.json must never be acted on."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def merge_app(data: dict, app: str, values: Mapping[str, str]) -> None:
    """Merge ``values`` into ``data[app]`` (existing keys preserved)."""
    current = data.get(app)
    if not isinstance(current, dict):
        current = {}
        data[app] = current
    current.update({str(k): str(v) for k, v in values.items()})
