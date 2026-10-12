#!/usr/bin/env python3
"""Provider registry loader (spec section 4).

The registry (registry/<slot>.yaml) is the single source of truth for which
implementations exist per slot, their status and where their code lives. Adding
a provider means adding an entry and (for native ones) a module.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parent.parent
REGISTRY_DIR = ROOT / "registry"
SCHEMA = ROOT / "schemas" / "registry.schema.json"

SLOTS = ["compute", "mesh", "ingress", "gate", "secrets", "backup", "platform", "user_auth", "ci", "registry", "forge"]


def load() -> dict:
    """Return {slot: {"contract": str, "implementations": {id: entry}}}."""
    registry: dict = {}
    for slot in SLOTS:
        path = REGISTRY_DIR / f"{slot}.yaml"
        if not path.exists():
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        registry[slot] = {
            "contract": data.get("contract", ""),
            "implementations": {impl["id"]: impl for impl in data.get("implementations", [])},
        }
    return registry


def schema_errors() -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    problems = []
    for slot in SLOTS:
        path = REGISTRY_DIR / f"{slot}.yaml"
        if not path.exists():
            problems.append(f"registry/{slot}.yaml: missing")
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for error in validator.iter_errors(data):
            location = "/".join(str(p) for p in error.path) or "<root>"
            problems.append(f"registry/{slot}.yaml: {location}: {error.message}")
    return problems


def check_selection(stack: dict, servers: dict) -> list[str]:
    """The selected slot implementations and server providers must exist and be
    implemented (or explicitly planned)."""
    registry = load()
    problems: list[str] = []

    for slot, impl_id in (stack.get("slots") or {}).items():
        impls = registry.get(slot, {}).get("implementations", {})
        if slot not in registry:
            problems.append(f"slots.{slot}: no registry file for slot")
            continue
        entry = impls.get(impl_id)
        if entry is None:
            problems.append(f"slots.{slot}: '{impl_id}' is not in the {slot} registry")
            continue
        problems.extend(_check_impl(slot, impl_id, entry))

    for name, spec in (servers.get("servers") or {}).items():
        provider = spec.get("provider")
        impls = registry.get("compute", {}).get("implementations", {})
        entry = impls.get(provider)
        if entry is None:
            problems.append(f"server '{name}': compute provider '{provider}' is not in the registry")
            continue
        problems.extend(_check_impl("compute", provider, entry))

    return problems


def _check_impl(slot: str, impl_id: str, entry: dict) -> list[str]:
    if entry.get("status") == "planned":
        return [f"{slot}:{impl_id} is registered but not implemented yet (planned)"]
    problems = []
    impl = entry.get("impl") or {}
    module = impl.get("tofu_module")
    if module and not (ROOT / "tofu" / "modules" / module).is_dir():
        problems.append(f"{slot}:{impl_id}: tofu module '{module}' not found")
    role = impl.get("ansible_role")
    if role and not (ROOT / "ansible" / "roles" / role).is_dir():
        problems.append(f"{slot}:{impl_id}: ansible role '{role}' not found")
    adapter = impl.get("adapter")
    if adapter and not (ROOT / adapter).exists():
        problems.append(f"{slot}:{impl_id}: adapter '{adapter}' not found")
    return problems


def find(slot: str, impl_id: str) -> dict | None:
    return load().get(slot, {}).get("implementations", {}).get(impl_id)


def creates_server(provider: str) -> bool:
    entry = find("compute", provider)
    return bool(entry and entry.get("creates_server"))


def release_status() -> tuple[int, int, float]:
    """(supported, total, ratio) across every registered implementation (D-027)."""
    registry = load()
    total = sum(len(data["implementations"]) for data in registry.values())
    supported = sum(
        1
        for data in registry.values()
        for entry in data["implementations"].values()
        if entry.get("status") == "supported"
    )
    ratio = (supported / total) if total else 0.0
    return supported, total, ratio


def _print_list(slot: str | None) -> None:
    registry = load()
    slots = [slot] if slot else SLOTS
    for name in slots:
        data = registry.get(name)
        if not data:
            continue
        print(f"\n{name}: {data['contract']}")
        for impl in data["implementations"].values():
            print(f"  [{impl['status']:8}] {impl['id']:20} {impl['name']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command")
    list_p = sub.add_parser("list")
    list_p.add_argument("slot", nargs="?")
    sub.add_parser("check")
    cap_p = sub.add_parser("capability", help="print a capability (e.g. creates_server) for a compute provider")
    cap_p.add_argument("provider")
    sub.add_parser("release-status", help="show the supported-provider ratio and the v1.0 gate")

    args = parser.parse_args()

    if args.command == "check":
        problems = schema_errors()
        for problem in problems:
            print(f"  fail  {problem}", file=sys.stderr)
        if problems:
            return 1
        print("registry: ok")
        return 0

    if args.command == "capability":
        print("true" if creates_server(args.provider) else "false")
        return 0

    if args.command == "release-status":
        supported, total, ratio = release_status()
        gate = ratio >= 0.5
        print(f"supported providers: {supported}/{total} ({ratio:.0%})")
        print(f"v1.0 gate (>= 50% supported): {'MET' if gate else 'not met'}")
        return 0 if gate else 1

    _print_list(getattr(args, "slot", None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
