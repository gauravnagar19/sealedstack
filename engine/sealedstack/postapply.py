"""``post-apply`` in Python (D-047).

After ``platform apply``:
  1. honour ``restore_on_create`` for a still-empty database (C1);
  2. merge infra-owned env keys into each compose, preserving the release-owned
     ``APP_VERSION`` (D-032, N9);
  3. trigger a first/deferred deploy for changed apps (H11);
  4. remove the secret-bearing rendered files (H10).

Replaces ``scripts/post-apply.sh``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import db as dbmod
from . import deployapps
from . import dokploy as dokploy_mod
from . import envfile
from . import envsync
from . import platformstate as platformstate_mod
from . import secrets as secrets_mod


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    client: Optional[dokploy_mod.DokployClient] = None,
    state: Optional[platformstate_mod.PlatformState] = None,
    db=None,
    out=None,
    sleep=None,
) -> None:
    import time

    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout

    apps_dir = Path(config.code_root) / ".build" / "apps"
    restore_apps = []
    for path in sorted(apps_dir.glob("*.json")):
        rendered = json.loads(path.read_text(encoding="utf-8") or "{}")
        postgres = ((rendered.get("manifest") or {}).get("needs") or {}).get("postgres") or {}
        if postgres.get("restore_on_create"):
            restore_apps.append(str(rendered.get("name") or path.stem))

    if restore_apps:
        db = db or dbmod.Db(config, env, source=source, state=state, client=client, out=out)
        for app in restore_apps:
            print(f"==> restore_on_create: {app}", file=out)
            db.restore(app, restore_on_create=True)

    print("==> env-sync", file=out)
    envsync.run(config, env, source=source, client=client, state=state, out=out)

    print("==> deploy-apps", file=out)
    deployapps.run(
        config,
        env,
        source=source,
        client=client,
        state=state,
        out=out,
        **({} if sleep is None else {"sleep": sleep}),
    )

    envfile.env_path(config.code_root).unlink(missing_ok=True)
    (Path(config.code_root) / ".build" / "edge-outputs.json").unlink(missing_ok=True)
    print("==> removed secret-bearing .build files", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        run(config, env)
    except (config_mod.ConfigError, dokploy_mod.DokployError, deployapps.DeployError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
