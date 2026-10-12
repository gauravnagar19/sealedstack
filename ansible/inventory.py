#!/usr/bin/env python3
"""Ansible dynamic inventory built from ``config/servers.yaml`` (spec section 3).

Servers are addressed by their Tailscale MagicDNS name, which is the key in
``servers.yaml``. The public IP is never stored here (D-008).
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
# Config may live outside the repo (D-020); SEALEDSTACK_CONFIG is the source of
# truth, defaulting to <repo>/config.
CONFIG_DIR = pathlib.Path(
    os.environ.get("SEALEDSTACK_CONFIG") or (ROOT / "config")
).expanduser()
SERVERS_FILE = CONFIG_DIR / "servers.yaml"
STACK_FILE = CONFIG_DIR / "stack.yaml"


def admin_user() -> str:
    if STACK_FILE.exists():
        stack = yaml.safe_load(STACK_FILE.read_text(encoding="utf-8")) or {}
        return stack.get("admin_user", "admin")
    return "admin"


def tailnet_suffix() -> str:
    """MagicDNS suffix (e.g. tail1234.ts.net) from stack.mesh.tailnet, if set.

    Fully-qualified names resolve inside the toolbox container even where the
    Mac's Tailscale DNS search domain is not available (H3).
    """
    if STACK_FILE.exists():
        stack = yaml.safe_load(STACK_FILE.read_text(encoding="utf-8")) or {}
        mesh = stack.get("mesh") or {}
        suffix = mesh.get("tailnet") or ""
        return suffix.lstrip(".")
    return ""


def build_inventory() -> dict:
    servers = (yaml.safe_load(SERVERS_FILE.read_text(encoding="utf-8")) or {}).get("servers", {})
    suffix = tailnet_suffix()
    inventory = {
        "_meta": {"hostvars": {}},
        "all": {"children": ["control", "workers"]},
        "control": {"hosts": []},
        "workers": {"hosts": []},
    }
    for name, spec in servers.items():
        group = "control" if spec.get("role") == "control" else "workers"
        host = f"{name}.{suffix}" if suffix else name
        inventory[group]["hosts"].append(name)
        inventory["_meta"]["hostvars"][name] = {
            "ansible_host": host,
            "ansible_user": admin_user(),
            # Scope host-key handling to managed hosts: accept an unknown key on
            # first contact (fresh rebuild) but still pin it afterwards. The
            # global check is deliberately NOT disabled in ansible.cfg.
            "ansible_ssh_common_args": "-o StrictHostKeyChecking=accept-new",
            "provider": spec.get("provider"),
            "os": spec.get("os"),
            "role": spec.get("role"),
        }
    return inventory


def main(argv: list[str]) -> int:
    if len(argv) > 1 and argv[1] == "--host":
        inventory = build_inventory()
        host = argv[2] if len(argv) > 2 else ""
        json.dump(inventory["_meta"]["hostvars"].get(host, {}), sys.stdout)
        return 0
    json.dump(build_inventory(), sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
