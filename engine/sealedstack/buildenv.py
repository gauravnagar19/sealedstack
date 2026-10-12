"""``build-env`` in Python (spec section 6, D-047).

For every rendered app (``.build/apps/<app>.json``) resolve ``manifest.secrets``
from the app-secret project (default ``prod``) into ``.build/env.json``: the env
map the platform stack injects as Dokploy environment variables.

Replaces ``scripts/build-env.sh``. A missing secret is fatal (N3); secret values
never touch a child's argv.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import envfile
from . import secrets as secrets_mod


class BuildEnvError(RuntimeError):
    """An app secret is missing or the source errored."""


def app_secret_keys(data: dict) -> list[str]:
    """The keys an app declares, from the rendered json (manifest wins)."""
    manifest = data.get("manifest") or {}
    keys = manifest.get("secrets")
    if keys is None:
        keys = data.get("secrets")
    return [str(key) for key in (keys or [])]


def build(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    project: str = "prod",
) -> tuple[dict, int]:
    """Return ``(env_map, app_count)``. Raises :class:`BuildEnvError`."""
    env = dict(env or os.environ)
    build_dir = Path(config.code_root) / ".build"
    apps_dir = build_dir / "apps"
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env, project=project)

    out: dict = {}
    count = 0
    for path in sorted(apps_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as exc:
            raise BuildEnvError(f"{path.name}: invalid JSON: {exc}") from exc
        app = str(data.get("name") or path.stem)
        keys = app_secret_keys(data)
        if not keys:
            continue
        values: dict[str, str] = {}
        for key in keys:
            source_key = config.app_secret_key(app, key)
            result = source.get(source_key)
            if not result.found:
                raise BuildEnvError(
                    f"{app}: secret '{source_key}' is {result.status}: {result.detail}"
                )
            values[key] = result.value
        envfile.merge_app(out, app, values)
        count += 1
    return out, count


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        data, count = build(config, env)
    except BuildEnvError as exc:
        print(f"build-env: {exc}", file=sys.stderr)
        return 1
    path = envfile.env_path(config.code_root)
    envfile.save(path, data)
    print(f"build-env: wrote {path} ({count} app(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
