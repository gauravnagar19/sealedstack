"""``app add`` / ``add-app``: place an app on the infra, and onboard its repo (spec 6.3, UX section 6).

Two modes:

* **Pull requests (CI, the normal way).** With a forge token and the infra repo
  known (``GH_TOKEN`` + ``GITHUB_REPOSITORY``), two pull requests are opened and
  nothing is pushed to ``main`` (D-037): one in the **app repo** ("Onboard to
  sealedstack": ``sealedstack.yaml``, compose fixes, release workflow) and one in
  the **infra repo** ("Add app <name>": the ``config/apps.yaml`` entry). The infra
  PR states the order: merge the app PR first.
* **Local.** Without a forge, or with ``--local``, the entry is written to the
  local ``config/apps.yaml`` and validated. Records placement and exposure only:
  no secrets, no IPs (D-012).
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import yaml

from . import __version__
from . import config as config_mod
from . import forge as forge_mod
from . import onboard as onboard_mod

APP_BRANCH = "sealedstack/onboard"


# --------------------------------------------------------------------------- #
# local mode
# --------------------------------------------------------------------------- #
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


# --------------------------------------------------------------------------- #
# pull-request mode
# --------------------------------------------------------------------------- #
@dataclass
class PrResult:
    infra_repo: str
    infra_pr: int
    app_repo: str
    app_pr: Optional[int]


def append_app_entry(text: str, name: str, entry: dict) -> str:
    """Add ``name: entry`` under ``apps:`` without re-serialising the file.

    Keeps the comments and the other apps byte for byte. Falls back to a plain
    re-dump when ``apps:`` is not a block mapping.
    """
    data = yaml.safe_load(text) or {}
    lines = text.split("\n")
    top = onboard_mod._top(lines, "apps")
    block_map = bool(top) and isinstance(data.get("apps"), dict) and not lines[top[0]].split(":", 1)[1].strip()
    if not block_map:
        data.setdefault("apps", {})
        if not isinstance(data["apps"], dict):
            raise config_mod.ConfigError("config/apps.yaml: 'apps' must be a mapping")
        data["apps"][name] = entry
        return yaml.safe_dump(data, sort_keys=False)
    children = onboard_mod._children(lines, top[0], top[1])
    indent = onboard_mod._indent(lines[children[0][1]]) if children else 2
    snippet = yaml.safe_dump({name: entry}, sort_keys=False, default_flow_style=False).rstrip("\n").split("\n")
    pad = " " * indent
    insert = ["", *[pad + line for line in snippet]] if children else [pad + line for line in snippet]
    end = top[1]
    lines[end:end] = insert
    result = "\n".join(lines)
    return result if result.endswith("\n") else result + "\n"


def default_server(servers: Mapping) -> str:
    table = (servers or {}).get("servers") or {}
    for name, spec in table.items():
        if (spec or {}).get("role") == "control":
            return name
    return next(iter(table), "")


def registry_prefix(config: config_mod.Config, app_repo: str) -> str:
    """``<registry>/<owner>`` the app's images are pushed to (D-033)."""
    slots = config.stack.get("slots") or {}
    impl = slots.get("registry") or "ghcr"
    providers = config.stack.get("providers") or {}
    block = providers.get(f"registry_{impl}") or {}
    urls = {"ghcr": "ghcr.io", "dockerhub": "docker.io", "quay": "quay.io"}
    url = block.get("url") or urls.get(impl, "ghcr.io")
    owner = block.get("owner") or block.get("namespace") or app_repo.split("/", 1)[0]
    return f"{url}/{str(owner).lower()}"


def read_optional(f: forge_mod.Forge, repo: str, path: str, ref: str) -> str:
    try:
        return f.read_file(repo, path, ref) or ""
    except forge_mod.ForgeError:
        return ""


def collect_app_files(f: forge_mod.Forge, repo: str, ref: str) -> dict[str, str]:
    """The files of an app repo that onboarding looks at (read through the forge)."""
    paths = [onboard_mod.MANIFEST_PATH, onboard_mod.OVERRIDE_PATH, *onboard_mod.COMPOSE_CANDIDATES]
    paths += f.list_files(repo, ".github/workflows", ref)
    files: dict[str, str] = {}
    for path in dict.fromkeys(paths):
        text = read_optional(f, repo, path, ref)
        if text:
            files[path] = text
    for text in list(files.values()):
        for script in re.findall(r"(?:\./)?(scripts/[\w./-]+\.sh)", text):
            if script not in files:
                body = read_optional(f, repo, script, ref)
                if body:
                    files[script] = body
    return files


def validate_infra_change(config: config_mod.Config, apps_text: str) -> list[str]:
    """Schema and cross-file problems the new apps.yaml would cause (before any PR)."""
    from . import preflight

    validate_mod, _registry, _render, _lint = preflight._load_script_modules(config.code_root)
    try:
        apps = yaml.safe_load(apps_text) or {}
    except yaml.YAMLError as exc:
        return [f"apps.yaml is not valid YAML: {exc}"]
    validate_mod.CONFIG = config.config_dir
    problems = list(validate_mod.schema_errors(apps, validate_mod.SCHEMAS / "apps.schema.json"))
    problems += validate_mod.check_cross_file(config.stack, config.servers, apps)
    return problems


def open_prs(
    f: forge_mod.Forge,
    config: config_mod.Config,
    *,
    infra_repo: str,
    name: str,
    app_repo: str,
    hosts: list[str],
    servers: list[str],
    access: str,
    ref: str = "main",
    web_service: str = "",
    web_port: int = 0,
    health: str = "",
    tool_owner: str = "",
    version: str = "",
    infra_base: str = "main",
    app_base: str = "main",
    out=None,
) -> PrResult:
    out = out if out is not None else sys.stdout
    apps = (config.apps.get("apps") or {})
    if name in apps:
        raise config_mod.ConfigError(f"app '{name}' already exists in config/apps.yaml")
    servers = servers or [default_server(config.servers)]
    if not servers[0]:
        raise config_mod.ConfigError("no server is configured in config/servers.yaml; add one first")

    entry = {
        "source": {"repo": app_repo, "ref": ref},
        "servers": list(servers),
        "hostnames": list(hosts),
        "access": access,
    }
    apps_path = config.config_dir / "apps.yaml"
    current = apps_path.read_text(encoding="utf-8") if apps_path.is_file() else "apps: {}\n"
    new_apps = append_app_entry(current, name, entry)
    problems = validate_infra_change(config, new_apps)
    if problems:
        raise config_mod.ConfigError("the new app entry is not valid: " + "; ".join(problems))

    # -- the app repo ------------------------------------------------------- #
    files = collect_app_files(f, app_repo, ref)
    tool_owner = tool_owner or infra_repo.split("/", 1)[0]
    version = version or __version__
    app_pr: Optional[int] = None
    manifest: dict = {}
    if onboard_mod.MANIFEST_PATH in files:
        manifest = yaml.safe_load(files[onboard_mod.MANIFEST_PATH]) or {}
        print(f"app-add: {app_repo} already has sealedstack.yaml; skipping the onboarding PR", file=out)
    else:
        plan = onboard_mod.generate(
            files,
            name=name,
            repo=app_repo,
            registry_prefix=registry_prefix(config, app_repo),
            tool_owner=tool_owner,
            version=version,
            web_service=web_service,
            web_port=web_port,
            health=health,
        )
        manifest = plan.manifest
        app_pr = f.open_pr(
            app_repo,
            branch=APP_BRANCH,
            title="Onboard to sealedstack",
            body=plan.body,
            files=plan.files,
            deletes=plan.deletes,
            base=app_base,
        )
        print(f"app-add: opened {app_repo}#{app_pr} (onboarding)", file=out)

    # -- the infra repo ----------------------------------------------------- #
    infra_pr = f.open_pr(
        infra_repo,
        branch=f"sealedstack/add-app-{name}",
        title=f"Add app {name}",
        body=render_infra_body(
            name=name, app_repo=app_repo, hosts=hosts, servers=servers, access=access, manifest=manifest, app_pr=app_pr
        ),
        files={"config/apps.yaml": new_apps},
        base=infra_base,
    )
    print(f"app-add: opened {infra_repo}#{infra_pr} (add app)", file=out)

    if app_pr is not None:
        f.upsert_comment(
            app_repo,
            app_pr,
            f"The infra pull request that places this app is {infra_repo}#{infra_pr}. "
            "**Merge this PR first**, then merge that one.",
        )
    return PrResult(infra_repo=infra_repo, infra_pr=infra_pr, app_repo=app_repo, app_pr=app_pr)


def render_infra_body(
    *,
    name: str,
    app_repo: str,
    hosts: list[str],
    servers: list[str],
    access: str,
    manifest: dict,
    app_pr: Optional[int],
) -> str:
    login = "anyone (public)" if access == "public" else access
    where = ", ".join(servers)
    urls = ", ".join(f"https://{h}" for h in hosts)
    lines = [f"Adds {name} on {where} at {urls} (login: {login}).", ""]
    lines.append("This adds, once applied:")
    lines.append(f"- a tunnel `ss-{name}`, DNS for {', '.join(hosts)}, and the Access app ({access})")
    lines.append("- a Dokploy project, compose service, domain and connector")
    needs = (manifest or {}).get("needs") or {}
    pg = needs.get("postgres")
    if pg:
        backup = pg.get("backup") or {}
        lines.append(
            f"- Postgres {pg.get('version')} ({pg.get('database')})"
            + (f", backups every {backup.get('every')} to R2" if backup else "")
        )
    if needs.get("redis"):
        lines.append("- Redis")
    lines += ["", "The plan comment below is written by the plan workflow."]
    if app_pr is not None:
        lines += [
            "",
            f"**Waiting on: {app_repo}#{app_pr} (onboarding). Merge that PR first**, then merge this one "
            "and approve the apply. The manifest is read from the app repo, so the plan only becomes "
            "complete after the onboarding PR is merged.",
        ]
    else:
        lines += ["", f"{app_repo} already has `sealedstack.yaml`, so there is no onboarding PR; merge this one and approve."]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# command line
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="app-add", description="Place an app and open the onboarding pull requests.")
    parser.add_argument("name", nargs="?", help="app name (or use --name)")
    parser.add_argument("--name", dest="name_opt")
    parser.add_argument("--repo", required=True, help="the app repository, owner/name")
    parser.add_argument("--host", action="append", default=[], dest="hosts", help="hostname (repeatable)")
    parser.add_argument("--server", action="append", default=[], dest="servers", help="server (repeatable; default: the control server)")
    parser.add_argument("--access", required=True, help="'public' or a gate policy from stack.yaml")
    parser.add_argument("--ref", default="main")
    parser.add_argument("--web-service", default="", help="compose service to route (default: detected)")
    parser.add_argument("--web-port", type=int, default=0, help="port that service listens on (default: detected)")
    parser.add_argument("--health", default="", help="health path, e.g. /api/health (default: detected)")
    parser.add_argument("--infra-repo", default="", help="owner/name of the infra repo (default: $GITHUB_REPOSITORY)")
    parser.add_argument("--local", action="store_true", help="write config/apps.yaml locally instead of opening pull requests")
    return parser


def main(argv: Optional[list[str]] = None, *, env: Optional[Mapping[str, str]] = None, forge: Optional[forge_mod.Forge] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    name = args.name_opt or args.name
    if not name:
        parser.error("the app name is required (positional or --name)")
    if not args.hosts:
        parser.error("--host is required (and repeatable)")

    env = dict(os.environ if env is None else env)
    code_root = config_mod.find_code_root(env)
    config_dir = config_mod.resolve_config_dir(env=env, code_root=code_root)
    config = config_mod.load_config(config_dir, code_root, env)
    infra_repo = args.infra_repo or env.get("GITHUB_REPOSITORY") or forge_mod.repo_from_git(config_dir)
    use_prs = not args.local and bool(infra_repo) and (forge is not None or bool(env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")))

    try:
        if use_prs:
            f = forge or forge_mod.GitHubForge.from_env(env)
            open_prs(
                f,
                config,
                infra_repo=infra_repo,
                name=name,
                app_repo=args.repo,
                hosts=args.hosts,
                servers=args.servers,
                access=args.access,
                ref=args.ref,
                web_service=args.web_service,
                web_port=args.web_port,
                health=args.health,
                tool_owner=env.get("SEALEDSTACK_PUBLISHER", ""),
            )
            return 0
        servers = args.servers or [default_server(config.servers)]
        add(config_dir, name=name, repo=args.repo, hosts=args.hosts, servers=servers, access=args.access, ref=args.ref)
    except (config_mod.ConfigError, onboard_mod.OnboardError, forge_mod.ForgeError) as exc:
        print(f"app-add: {exc}", file=sys.stderr)
        return 2
    print("app-add: validating")
    return subprocess.run([sys.executable, str(code_root / "scripts" / "validate.py"), "--allow-unresolved"]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
