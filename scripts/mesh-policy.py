#!/usr/bin/env python3
"""Merge sealedstack's block into your Tailscale policy (spec section 7, D-026).

"Your file, our block": ``config/tailscale-policy.hujson`` is the user's own
tailnet policy, imported verbatim by ``sealedstack setup``. This script parses it
as HuJSON (a JSON5-compatible dialect, so ``//`` comments and trailing commas
work), appends only sealedstack's tags, rules and tests, and writes the effective
policy JSON to ``.build/tailscale-policy.json``. Every other section passes
through untouched, so the rest of the tailnet keeps working.

The edge stack reads the rendered JSON (``file(...)``); run this before
``plan``/``apply``. If the file is missing and ``mesh.policy`` is ``managed``
(the default) this exits non-zero; with ``mesh.policy: unmanaged`` it prints the
block for manual pasting and exits 0.

F1: a default tailnet still carries the allow-all rule
``{"src": ["*"], "dst": ["*"], "ip": ["*"]}``. That lets any device, including
``tag:ci``, reach every port on a server, so Tailscale rejects sealedstack's
deny-22 test and the servers are not actually sealed. When such a broad rule is
present this script **refuses** with a clear message. Set
``mesh.narrow_allow_all: true`` to let it rewrite the exact ``* -> *`` rule to
``autogroup:member -> autogroup:member:*`` (members' own devices only; tagged
servers are excluded). Any other broad destination is refused and must be
scoped by hand.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import json5
import yaml

import configdir

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = ROOT / ".build"
CONFIG = configdir.config_dir()

POLICY_FILE = "tailscale-policy.hujson"
OUTPUT_FILE = "tailscale-policy.json"

# Sections that sealedstack never touches.
PASSTHROUGH = ("groups", "hosts", "ssh", "nodeAttrs", "autoApprovers", "postures")

SEALEDSTACK_TAG_OWNERS = {
    "tag:server": ["autogroup:admin"],
    "tag:ci": ["autogroup:admin"],
}

# Destinations that match every device, including tagged servers. A rule that
# accepts to one of these makes every sealedstack rule redundant and lets any
# device (including tag:ci) reach every port on a server, so sealedstack refuses
# to manage a policy that contains one (F1).
BROAD_DESTS = {"*", "*:*", "autogroup:*"}

# The rewrite offered for an exact allow-all rule: members' own devices only.
# Tagged devices are not `autogroup:member`, so tagged servers are excluded and
# sealedstack's server grants become the only way in.
NARROWED_SRC = "autogroup:member"
NARROWED_DST = "autogroup:member:*"


def sealedstack_acls() -> list[dict]:
    return [
        {"action": "accept", "src": ["autogroup:admin"], "dst": ["tag:server:*"]},
        {"action": "accept", "src": ["tag:ci"], "dst": ["tag:server:3000"]},
        {"action": "accept", "src": ["tag:server"], "dst": ["tag:server:5432"]},
    ]


def sealedstack_grants() -> list[dict]:
    # Grants imply accept; the port belongs in `ip`, not in `dst` (grants-syntax).
    return [
        {"src": ["autogroup:admin"], "dst": ["tag:server"], "ip": ["*"]},
        {"src": ["tag:ci"], "dst": ["tag:server"], "ip": ["tcp:3000"]},
        {"src": ["tag:server"], "dst": ["tag:server"], "ip": ["tcp:5432"]},
    ]


def sealedstack_tests() -> list[dict]:
    return [
        {"src": ["autogroup:admin"], "accept": ["tag:server:22"]},
        {"src": ["tag:ci"], "deny": ["tag:server:22"]},
        {"src": ["tag:ci"], "accept": ["tag:server:3000"]},
    ]


def sealedstack_block() -> dict:
    return {
        "tagOwners": dict(SEALEDSTACK_TAG_OWNERS),
        "acls": sealedstack_acls(),
        "tests": sealedstack_tests(),
    }


def _dst_values(rule) -> list[str]:
    if not isinstance(rule, dict):
        return []
    dst = rule.get("dst")
    if dst is None:
        return []
    if isinstance(dst, str):
        return [dst]
    if isinstance(dst, list):
        return [str(item) for item in dst]
    return []


def _src_values(rule) -> list[str]:
    if not isinstance(rule, dict):
        return []
    src = rule.get("src")
    if isinstance(src, str):
        return [src]
    if isinstance(src, list):
        return [str(item) for item in src]
    return []


def broad_rules(policy: dict) -> list[tuple[str, dict]]:
    """Rules whose ``dst`` covers every device, including tagged servers (F1).

    Covers the exact allow-all rule (``src * -> dst *``) and any other rule that
    accepts to ``*`` / ``autogroup:*``. Such a rule lets any device reach every
    port on a sealedstack server, so the tailnet is not actually sealed and
    Tailscale may reject sealedstack's ``tag:ci`` deny-22 test.
    """
    found: list[tuple[str, dict]] = []
    for key in ("acls", "grants"):
        rules = policy.get(key)
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if any(dst in BROAD_DESTS for dst in _dst_values(rule)):
                found.append((key, rule))
    return found


def is_allow_all(rule: dict) -> bool:
    """The exact ``{"src": ["*"], "dst": ["*"]}`` rule (an allow-all grant)."""
    return _src_values(rule) == ["*"] and _dst_values(rule) in (["*"], ["*:*"])


def narrow_broad_rules(policy: dict) -> list[tuple[str, dict]]:
    """Rewrite every exact allow-all rule to members-only. Returns the rewrites."""
    rewrites: list[tuple[str, dict]] = []
    for key, rule in broad_rules(policy):
        if not is_allow_all(rule):
            continue
        before = {"src": list(_src_values(rule)), "dst": list(_dst_values(rule))}
        after_dst = NARROWED_DST if key == "acls" else NARROWED_SRC
        rule["src"] = [NARROWED_SRC]
        # ACLs carry the port in `dst` (host:port); grants keep it in `ip`.
        rule["dst"] = [after_dst]
        rewrites.append((key, before, after_dst))
    return rewrites


def load_yaml(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    path.chmod(0o600)


def append_unique(target: list, items: list, added: list, *, key: str) -> None:
    for item in items:
        if item not in target:
            target.append(item)
            added.append((key, item))


def merge(policy: dict) -> tuple[dict, dict]:
    """Append sealedstack's block to ``policy`` in place. Returns (policy, added)."""
    added: dict = {"tagOwners": [], "rules": [], "tests": []}

    owners = policy.get("tagOwners")
    if not isinstance(owners, dict):
        owners = {}
        policy["tagOwners"] = owners
    for tag, value in SEALEDSTACK_TAG_OWNERS.items():
        if tag not in owners:
            owners[tag] = value
            added["tagOwners"].append(tag)

    # Grants if the file uses grants, otherwise legacy ACLs.
    if isinstance(policy.get("grants"), list):
        key, new_rules = "grants", sealedstack_grants()
    else:
        key, new_rules = "acls", sealedstack_acls()
    rules = policy.get(key)
    if not isinstance(rules, list):
        rules = []
        policy[key] = rules
    append_unique(rules, new_rules, added["rules"], key=key)

    tests = policy.get("tests")
    if not isinstance(tests, list):
        tests = []
        policy["tests"] = tests
    append_unique(tests, sealedstack_tests(), added["tests"], key="tests")

    return policy, added


def describe_rule(key: str, rule: dict) -> str:
    src = "/".join(rule.get("src", []))
    if key == "grants":
        dst = "/".join(rule.get("dst", []))
        ip = "/".join(rule.get("ip", []))
        return f"grant  {src} -> {dst} [{ip}]"
    action = rule.get("action", "accept")
    dst = "/".join(rule.get("dst", []))
    return f"{action:<6} {src} -> {dst}"


def describe_test(rule: dict) -> str:
    src = "/".join(rule.get("src", []))
    if "deny" in rule:
        return f"deny   {src} -> {'/'.join(rule['deny'])}"
    return f"accept {src} -> {'/'.join(rule.get('accept', []))}"


def print_summary(added: dict, source: pathlib.Path, output: pathlib.Path) -> None:
    print(f"mesh-policy: merged {source} -> {output}")
    if added["tagOwners"]:
        print(f"  tagOwners: added {', '.join(added['tagOwners'])} (existing keys kept)")
    else:
        print("  tagOwners: sealedstack tags already present")
    if added["rules"]:
        print(f"  {added['rules'][0][0]}: appended {len(added['rules'])} rule(s):")
        for key, rule in added["rules"]:
            print(f"    {describe_rule(key, rule)}")
    else:
        print("  rules: sealedstack rules already present")
    if added["tests"]:
        print(f"  tests: appended {len(added['tests'])} test(s):")
        for _key, rule in added["tests"]:
            print(f"    {describe_test(rule)}")
    else:
        print("  tests: sealedstack tests already present")


def print_unmanaged_block() -> None:
    print(
        "mesh-policy: config/tailscale-policy.hujson is missing and mesh.policy is\n"
        "             'unmanaged'. sealedstack will not manage your tailnet policy.\n"
        "             Paste this block into your policy by hand:\n"
    )
    print(json.dumps(sealedstack_block(), indent=2, sort_keys=False))


def _describe_broad(key: str, rule: dict) -> str:
    return f"  {key:6} {describe_rule(key, rule)}"


def print_broad_refusal(broad: list[tuple[str, dict]], source: pathlib.Path) -> None:
    print(
        f"mesh-policy: {source} contains a rule that allows every device to reach\n"
        "             every port on sealedstack's servers (a default allow-all\n"
        "             tailnet). sealedstack will not manage a policy that leaves\n"
        "             `tag:server` wide open: any device, including `tag:ci`, could\n"
        "             reach port 22 and the tailnet would reject the deny-22 test.\n"
        "\n"
        "             Offending rule(s):",
        file=sys.stderr,
    )
    for key, rule in broad:
        print(_describe_broad(key, rule), file=sys.stderr)
    print(
        "\n"
        "             Narrow the allow-all rule(s) to members' own devices:\n"
        f"               acls:   \"src\": [\"{NARROWED_SRC}\"], \"dst\": [\"{NARROWED_DST}\"]\n"
        f"               grants: \"src\": [\"{NARROWED_SRC}\"], \"dst\": [\"{NARROWED_SRC}\"] (port stays in \"ip\")\n"
        "             (`autogroup:member` excludes tagged devices, so tagged\n"
        "             servers are served only by sealedstack's own grants.)\n"
        "\n"
        "             Edit config/tailscale-policy.hujson by hand, or let\n"
        "             sealedstack do it by setting, in config/stack.yaml:\n"
        "               mesh:\n"
        "                 narrow_allow_all: true\n"
        "             then re-run `plan`/`apply` and review the printed rewrite.",
        file=sys.stderr,
    )
    print(
        "\n"
        "             Note: a broad rule other than the exact `* -> *` allow-all\n"
        "             (`autogroup:*`, a wildcard host) cannot be rewritten safely;\n"
        "             scope it to your own devices or groups by hand.",
        file=sys.stderr,
    )


def print_narrowed(rewrites: list[tuple[str, dict, str]]) -> None:
    print(f"mesh-policy: narrowing {len(rewrites)} allow-all rule(s) to members only:")
    for key, before, after_dst in rewrites:
        src = "/".join(before["src"])
        dst = "/".join(before["dst"])
        print(f"  {key:6} {src} -> {dst}   ==>   {NARROWED_SRC} -> {after_dst}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config-dir",
        default=None,
        help="config directory (default: $SEALEDSTACK_CONFIG, else <repo>/config)",
    )
    args = parser.parse_args()

    global CONFIG
    if args.config_dir:
        CONFIG = pathlib.Path(args.config_dir).expanduser()

    stack = load_yaml(CONFIG / "stack.yaml")
    slot = (stack.get("slots") or {}).get("mesh")
    mesh = stack.get("mesh") or {}
    policy_mode = mesh.get("policy", "managed")
    narrow_allow_all = bool(mesh.get("narrow_allow_all", False))

    source = CONFIG / POLICY_FILE
    output = BUILD / OUTPUT_FILE

    if not source.exists():
        if policy_mode == "unmanaged":
            print_unmanaged_block()
            # Keep the edge stack's file() happy even though nothing is managed.
            write_json(output, sealedstack_block())
            return 0
        if slot != "tailscale":
            # Not a Tailscale mesh: no policy to merge, but the edge stack still
            # evaluates the policy file.
            write_json(output, {})
            print(f"mesh-policy: mesh slot is '{slot}', not tailscale; wrote empty {output}")
            return 0
        print(
            f"mesh-policy: {source} is missing.\n"
            "             mesh.policy is 'managed' (default), so sealedstack must merge\n"
            "             its block into your tailnet policy. Run `sealedstack setup` to\n"
            "             import it, create the file, or set `mesh.policy: unmanaged`.",
            file=sys.stderr,
        )
        return 1

    try:
        policy = json5.loads(source.read_text(encoding="utf-8"))
    except ValueError as exc:
        print(f"mesh-policy: could not parse {source} (HuJSON): {exc}", file=sys.stderr)
        return 1

    if not isinstance(policy, dict):
        print(f"mesh-policy: {source} must contain a JSON object at the top level", file=sys.stderr)
        return 1

    # F1: a default allow-all tailnet makes sealedstack's servers unreachable to
    # lock down and makes Tailscale reject the deny-22 test. Refuse (or narrow on
    # request) before merging.
    broad = broad_rules(policy)
    if broad and policy_mode != "unmanaged":
        if narrow_allow_all:
            rewrites = narrow_broad_rules(policy)
            if rewrites:
                print_narrowed(rewrites)
            remaining = broad_rules(policy)
            if remaining:
                print_broad_refusal(remaining, source)
                return 1
        else:
            print_broad_refusal(broad, source)
            return 1
    elif broad:
        print(
            "mesh-policy: warning: the policy contains an allow-all rule, but\n"
            "             mesh.policy is 'unmanaged' so sealedstack does not apply\n"
            "             it. Your servers are not sealed until you scope it yourself.",
            file=sys.stderr,
        )

    present = [key for key in PASSTHROUGH if key in policy]
    policy, added = merge(policy)
    write_json(output, policy)
    print_summary(added, source, output)
    if present:
        print(f"  passthrough: {', '.join(present)} (kept untouched)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
