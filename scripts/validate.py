#!/usr/bin/env python3
"""Validate ``config/`` before any command runs (spec section 10).

Checks each config file against ``schemas/``, runs the cross-file rules, then
renders the app manifests and checks them against the manifest schema. Exits
non-zero on the first failing category so CI and ``sealedstack check`` can gate on
it.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import yaml
from jsonschema import Draft202012Validator, FormatChecker

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCHEMAS = ROOT / "schemas"

sys.path.insert(0, str(ROOT / "scripts"))
import configdir  # noqa: E402
import lint_compose as lint_mod  # noqa: E402
import registry as reg_mod  # noqa: E402
import render as render_mod  # noqa: E402

CONFIG = configdir.config_dir()


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warn(self, message: str) -> None:
        self.warnings.append(message)

    def check(self, label: str, problems: list[str]) -> None:
        if problems:
            for problem in problems:
                self.error(f"{label}: {problem}")
        else:
            print(f"  ok    {label}")


def load_yaml(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def schema_errors(instance: dict, schema_path: pathlib.Path) -> list[str]:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    problems = []
    for error in sorted(validator.iter_errors(instance), key=lambda e: list(e.path)):
        location = "/".join(str(part) for part in error.path) or "<root>"
        problems.append(f"{location}: {error.message}")
    return problems


def check_cross_file(stack: dict, servers: dict, apps: dict) -> list[str]:
    problems: list[str] = []
    domain = stack.get("domain", "")
    server_map = servers.get("servers") or {}
    policy_names = set((stack.get("gate") or {}).get("policies") or {})
    slot_auth = (stack.get("slots") or {}).get("user_auth")

    controls = [name for name, spec in server_map.items() if spec.get("role") == "control"]
    if len(controls) != 1:
        problems.append(f"expected exactly one control server, found {len(controls)}: {controls}")

    # WAF guard: the edge builds `ends_with(uri, substr(path, 1))` for entries
    # starting with `*`, so a bare `*` (or `**`, or empty) matches every path and
    # would block all traffic. Reject it here with a clear message.
    for path in (stack.get("edge_security") or {}).get("blocked_paths") or []:
        if not path.strip().strip("*"):
            problems.append(
                f"edge_security.blocked_paths: '{path}' would block all traffic "
                "(a bare '*'/'**' matches every URI); use a specific suffix like '*.php'"
            )

    # D-061: the key-based first login is in the schema for later but not implemented.
    for name, spec in server_map.items():
        if ((spec or {}).get("bootstrap") or {}).get("method") == "key":
            problems.append(f"server '{name}': bootstrap.method: key is not implemented yet; use password (D-061)")

    for name, entry in (apps.get("apps") or {}).items():
        # A paused app (enabled: false) is not rendered and must not be held to
        # the cross-file rules (hostnames, servers, policies).
        if entry.get("enabled") is False:
            continue

        access = entry.get("access")
        if access != "public" and access not in policy_names:
            problems.append(
                f"app '{name}': access '{access}' is neither 'public' nor a policy in stack.yaml"
            )

        for server in entry.get("servers", []):
            if server not in server_map:
                problems.append(f"app '{name}': unknown server '{server}'")

        hostnames = entry.get("hostnames", [])
        for hostname in hostnames:
            if hostname != domain and not hostname.endswith("." + domain):
                problems.append(f"app '{name}': hostname '{hostname}' is not under '{domain}'")

        for source, target in (entry.get("redirects") or {}).items():
            if source not in hostnames:
                problems.append(f"app '{name}': redirect source '{source}' is not in hostnames")
            if target not in hostnames:
                problems.append(f"app '{name}': redirect target '{target}' is not in hostnames")

        if entry.get("user_auth") and not slot_auth:
            problems.append(
                f"app '{name}': user_auth set but stack.yaml has no slots.user_auth"
            )

    # D-026: the user's imported policy is the source of truth; sealedstack only
    # merges its block into it. Refuse to plan without it unless explicitly
    # unmanaged, and require the MagicDNS suffix for fully-qualified hostnames.
    if (stack.get("slots") or {}).get("mesh") == "tailscale":
        mesh = stack.get("mesh") or {}
        if not mesh.get("tailnet"):
            problems.append(
                "mesh: slots.mesh=tailscale requires mesh.tailnet (the MagicDNS "
                "suffix, e.g. tail1234.ts.net)"
            )
        policy_file = CONFIG / "tailscale-policy.hujson"
        if mesh.get("policy", "managed") != "unmanaged" and not policy_file.exists():
            problems.append(
                f"mesh: {policy_file} is missing; run `sealedstack setup` to import "
                "your tailnet policy, create the file, or set mesh.policy: unmanaged"
            )

    return problems


def github_available() -> bool:
    """True when a GitHub token is set, so strict manifest resolution is meaningful."""
    return bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))


# Best-effort RAM lookup for the `build.mode: server` warning (spec 4.2). The
# servers schema carries no memory field yet (a schema change is a follow-up), so
# we read an explicit value if one exists and otherwise map well-known managed
# instance slugs. Unknown servers produce no warning rather than a wrong one.
_KNOWN_SERVER_MEMORY_GB = {
    # Hetzner Cloud shared
    "cx22": 4, "cx23": 4, "cx32": 8, "cx33": 8, "cx42": 16, "cx52": 32,
    "cax11": 2, "cax21": 4, "cax31": 8, "cax41": 16,
    "cpx11": 2, "cpx21": 4, "cpx31": 8, "cpx41": 16, "cpx51": 32,
    # DigitalOcean basic
    "s-1vcpu-1gb": 1, "s-1vcpu-2gb": 2, "s-2vcpu-2gb": 2,
    "s-2vcpu-4gb": 4, "s-4vcpu-8gb": 8,
    # Vultr
    "vc2-1c-1gb": 1, "vc2-1c-2gb": 2, "vc2-2c-2gb": 2, "vc2-2c-4gb": 4,
}


def server_memory_gb(server: str, servers: dict, stack: dict) -> float | None:
    spec = (servers.get("servers") or {}).get(server) or {}
    explicit = spec.get("memory_gb", spec.get("ram_gb"))
    if isinstance(explicit, (int, float)):
        return float(explicit)
    provider = spec.get("provider", "")
    cfg = (stack.get("providers") or {}).get(f"compute_{provider}") or {}
    configured = cfg.get("memory_gb", cfg.get("ram_gb"))
    if isinstance(configured, (int, float)):
        return float(configured)
    slug = spec.get("server_type") or spec.get("plan") or ""
    return _KNOWN_SERVER_MEMORY_GB.get(slug)


def repo_is_private(repo: str) -> bool | None:
    """True/False from the GitHub API, or None when it cannot be determined."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        return None
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - offline / no token: skip the check
        return None
    return bool(data.get("private"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat an unresolved (but enabled) app as an error",
    )
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="never treat an unresolved app as an error (offline use)",
    )
    args = parser.parse_args()

    # Offline (no `gh`), an unresolved private manifest is expected: warn. Once
    # `gh` is available, unresolved-and-enabled is a hard error (C2), because
    # plan/apply would otherwise drop the app and destroy its resources.
    strict = args.strict or (github_available() and not args.allow_unresolved)

    report = Report()

    print("validate: schema")
    stack = load_yaml(CONFIG / "stack.yaml")
    servers = load_yaml(CONFIG / "servers.yaml")
    apps = load_yaml(CONFIG / "apps.yaml")
    report.check("config/stack.yaml", schema_errors(stack, SCHEMAS / "stack.schema.json"))
    report.check("config/servers.yaml", schema_errors(servers, SCHEMAS / "servers.schema.json"))
    report.check("config/apps.yaml", schema_errors(apps, SCHEMAS / "apps.schema.json"))

    print("validate: cross-file rules")
    report.check("config consistency", check_cross_file(stack, servers, apps))

    print("validate: registry")
    report.check("registry files", reg_mod.schema_errors())
    report.check("provider selection", reg_mod.check_selection(stack, servers))

    print("validate: render")
    try:
        rendered = render_mod.render_all(allow_unresolved=True)
    except Exception as exc:  # noqa: BLE001
        report.error(f"render failed: {exc}")
        rendered = {}

    manifest_problems = []
    compose_problems = []
    for name, app in rendered.items():
        if app.get("_pending"):
            print(f"  skip  app '{name}': pending, its repo has no sealedstack.yaml yet")
            continue
        if app.get("_disabled"):
            print(f"  skip  app '{name}': disabled (enabled: false)")
            continue
        if app["_unresolved"]:
            message = (
                f"app '{name}': manifest unresolved ({app.get('_reason', '')}) "
                "— run with GitHub access, or set `enabled: false` to pause it"
            )
            if strict:
                report.error(message)
            else:
                report.warn(message + " (offline: not fatal here)")
            continue
        for problem in schema_errors(app["manifest"], SCHEMAS / "sealedstack.schema.json"):
            manifest_problems.append(f"app '{name}': {problem}")
        if app.get("_compose_error"):
            report.warn(f"app '{name}': could not fetch compose ({app['_compose_error']})")
        if app.get("_compose"):
            for problem in lint_mod.lint(app["_compose"], app.get("needs")):
                compose_problems.append(f"app '{name}': {problem}")
            for problem in lint_mod.lint_security(app["_compose"]):
                compose_problems.append(f"app '{name}': {problem}")
        # build.mode compose rules (spec 4.2): the ci image pin, checked while
        # rendering the deploy compose.
        for problem in app.get("_build_problems") or []:
            compose_problems.append(f"app '{name}': {problem}")

        # build.mode: server (spec 4.2/4.4): warn on a small box, fail on a
        # private repo with no Dokploy git provider.
        if app.get("_build_mode") == "server":
            for server in app.get("servers") or []:
                gb = server_memory_gb(server, servers, stack)
                if gb is not None and gb < 4:
                    report.warn(
                        f"app '{name}': server '{server}' has {gb:g} GB RAM; "
                        "build.mode 'server' builds on the box and may OOM (spec 4.2)"
                    )
            repo = (app.get("source") or {}).get("repo", "")
            if repo and not (stack.get("git_provider") or {}):
                private = repo_is_private(repo)
                if private:
                    report.error(
                        f"app '{name}': repo '{repo}' is private and stack.yaml has "
                        "no git_provider; Dokploy cannot clone it in build.mode 'server' (spec 4.4)"
                    )

        # H6: replicas on other servers cannot reach servers[0]'s database yet.
        needs = app.get("needs") or {}
        if needs.get("postgres") and len(app.get("servers") or []) > 1:
            report.error(
                f"app '{name}': multiple `servers` with needs.postgres is not supported "
                "yet (the database only resolves on servers[0])"
            )
    report.check("rendered manifests", manifest_problems)

    print("validate: compose rules")
    report.check("compose rules", compose_problems)

    print()
    for warning in report.warnings:
        print(f"  warn  {warning}")
    if report.errors:
        for error in report.errors:
            print(f"  fail  {error}", file=sys.stderr)
        print(f"\nvalidate: {len(report.errors)} error(s)", file=sys.stderr)
        return 1

    print("validate: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
