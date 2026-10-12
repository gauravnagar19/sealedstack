"""``app-add`` in Python (spec 6.3, D-047).

Append an app to ``config/apps.yaml`` and validate it. ``--host`` and
``--server`` may repeat; ``--ref`` defaults to ``main``. Records placement and
exposure only: no secrets, no IPs (D-012). Replaces ``scripts/app-add``.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import yaml

from . import config as config_mod


def add(
    config_dir: Path,
    *,
    name: str,
    repo: str,
    hosts: list[str],
    servers: list[str],
    access: str,
    ref: str = "main",
    out=None,
) -> None:
    out = out if out is not None else sys.stdout
    apps_file = Path(config_dir) / "apps.yaml"
    data = config_mod.load_yaml(apps_file)
    apps = data.setdefault("apps", {})
    if name in apps:
        raise config_mod.ConfigError(f"app '{name}' already exists in {apps_file}")
    apps[name] = {
        "source": {"repo": repo, "ref": ref},
        "servers": list(servers),
        "hostnames": list(hosts),
        "access": access,
    }
    apps_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    print(f"app-add: added '{name}' to {apps_file}", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="app-add", add_help=True)
    parser.add_argument("name")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--host", action="append", default=[], dest="hosts")
    parser.add_argument("--server", action="append", default=[], dest="servers")
    parser.add_argument("--access", required=True)
    parser.add_argument("--ref", default="main")
    args = parser.parse_args(argv)
    if not args.hosts or not args.servers:
        parser.error("--host and --server are required (and repeatable)")

    env = dict(os.environ)
    code_root = config_mod.find_code_root(env)
    config_dir = config_mod.resolve_config_dir(env=env, code_root=code_root)
    try:
        add(config_dir, name=args.name, repo=args.repo, hosts=args.hosts, servers=args.servers, access=args.access, ref=args.ref)
    except config_mod.ConfigError as exc:
        print(f"app-add: {exc}", file=sys.stderr)
        return 2
    print("app-add: validating")
    return subprocess.run([sys.executable, str(code_root / "scripts" / "validate.py"), "--allow-unresolved"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
