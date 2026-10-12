#!/usr/bin/env python3
"""Check that the selected providers' requirements are met (spec section 10).

Reads the registry, config/stack.yaml and config/servers.yaml, then reports any
missing secrets or tools. Read-only.
"""
from __future__ import annotations

import os
import shutil
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "engine"))
import configdir  # noqa: E402
import registry as reg  # noqa: E402
import validate as val  # noqa: E402
from sealedstack import secrets as ssecrets  # noqa: E402

CONFIG = configdir.config_dir()
SOURCE = ssecrets.build_source(val.load_yaml(CONFIG / "stack.yaml"), CONFIG, os.environ)


def secret_lookup(name: str) -> tuple[bool, str]:
    """Return (ok, value).

    ``ok`` is False only when the lookup itself failed (auth/network), so a
    missing secret is not confused with a broken secrets session (N3).
    """
    if os.environ.get(name):
        return True, os.environ[name]
    if SOURCE.kind == "bitwarden" and not os.environ.get("BWS_ACCESS_TOKEN"):
        return True, ""
    result = SOURCE.get(name)
    if result.found:
        return True, result.value
    if result.missing:
        return True, ""
    return False, result.detail or "secret lookup failed"


errors: list[str] = []


def main() -> int:
    stack = val.load_yaml(CONFIG / "stack.yaml")
    servers = val.load_yaml(CONFIG / "servers.yaml")
    registry = reg.load()

    missing_secrets: list[str] = []
    seen: set[str] = set()

    selections = []
    for slot, impl_id in (stack.get("slots") or {}).items():
        selections.append(("compute" if slot == "compute" else slot, impl_id))
    for name, spec in (servers.get("servers") or {}).items():
        selections.append(("compute", spec.get("provider")))

    for slot, impl_id in selections:
        entry = registry.get(slot, {}).get("implementations", {}).get(impl_id)
        if not entry or entry.get("status") == "planned":
            continue
        for secret in entry.get("secrets", []):
            if secret in seen:
                continue
            seen.add(secret)
            ok, value = secret_lookup(secret)
            if not ok:
                errors.append(f"{slot}:{impl_id} -> {secret}: {value}")
            elif not value:
                missing_secrets.append(f"{slot}:{impl_id} -> {secret}")

    # state_passphrase must be long enough for OpenTofu's pbkdf2 key provider
    # before any `init` (N15).
    passphrase_ok, passphrase = secret_lookup("state_passphrase")
    if not passphrase_ok:
        errors.append(f"state_passphrase: {passphrase}")
    short_passphrase = passphrase_ok and bool(passphrase) and len(passphrase) < 16

    tools = ["tofu", "ansible-playbook", "bws"]
    missing_tools = [tool for tool in tools if shutil.which(tool) is None]

    print("doctor: secrets")
    if errors:
        for item in errors:
            print(f"  error    {item}")
    if missing_secrets:
        for item in missing_secrets:
            print(f"  missing  {item}")
    if not errors and not missing_secrets:
        print("  ok       all required secrets present")

    print("doctor: state")
    if not passphrase_ok:
        print("  error    could not read state_passphrase")
    elif short_passphrase:
        print(f"  invalid  state_passphrase is {len(passphrase)} chars; pbkdf2 needs at least 16")
    elif passphrase:
        print("  ok       state_passphrase is at least 16 characters")
    else:
        print("  ok       state_passphrase not set (env fallback)")

    print("doctor: tools")
    if missing_tools:
        for tool in missing_tools:
            print(f"  missing  {tool}")
    else:
        print("  ok       all tools present")

    # Config checks (spec §6.3, §7): mesh tailnet + policy, registry pull token.
    config_problems: list[str] = []
    slots = stack.get("slots") or {}
    mesh = stack.get("mesh") or {}
    if slots.get("mesh") == "tailscale":
        if not mesh.get("tailnet"):
            config_problems.append("mesh.tailnet is not set (needed for FQDNs, spec §7.5)")
        if mesh.get("policy", "managed") == "managed" and not (CONFIG / "tailscale-policy.hujson").exists():
            config_problems.append(
                "mesh.policy is 'managed' but config/tailscale-policy.hujson is missing "
                "(run `sealedstack setup` to import it, or set mesh.policy: unmanaged)"
            )
    if slots.get("registry"):
        ok, value = secret_lookup("REGISTRY_PULL_TOKEN")
        if not ok:
            config_problems.append(f"REGISTRY_PULL_TOKEN: {value}")
        elif not value:
            config_problems.append("REGISTRY_PULL_TOKEN is missing (the platform needs it to pull images)")

    print("doctor: config")
    if config_problems:
        for item in config_problems:
            print(f"  problem  {item}")
    else:
        print("  ok       mesh and registry config look sane")

    return 1 if (missing_secrets or missing_tools or errors or short_passphrase or not passphrase_ok or config_problems) else 0


if __name__ == "__main__":
    raise SystemExit(main())
