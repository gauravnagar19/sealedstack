#!/usr/bin/env python3
"""Render app manifests into ``.build/``.

Merges each app's placement entry (``config/apps.yaml``) with its portable
manifest (``sealedstack.yaml`` in the app repo at ``source.ref``) and writes
``.build/apps/<app>.json``. The OpenTofu stacks read only ``.build/`` so a plan
is reproducible from a rendered snapshot (spec section 5.5).

For ``build.mode: ci`` apps (the default, D-030) it also renders a **deploy
compose**: the app compose with every ``build:`` removed and every previously
built service required to pin
``image: <registry>/<owner>/<repo>-<service>:${APP_VERSION}``. That file is
written to ``.build/apps/<app>.deploy-compose.yml`` and embedded in the app json
so the platform stack can hand it to Dokploy as raw compose source (Dokploy
never clones or builds). The selected ``registry`` slot (D-033) is rendered into
``.build/registry.json``.

Image-only apps (no repo) get a synthesized manifest. Repo apps whose manifest
cannot be fetched are a hard error by default (C2): a transient GitHub failure
must not drop an app from the plan and destroy its resources. Pass
``--allow-unresolved`` only for an offline/static check. Set ``enabled: false``
on an app in ``config/apps.yaml`` to deliberately pause it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

import yaml

import configdir
import lint_compose

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = configdir.config_dir()
# SEALEDSTACK_BUILD_DIR keeps test runs from writing into the repo's own .build/.
BUILD = pathlib.Path(os.environ.get("SEALEDSTACK_BUILD_DIR") or ROOT / ".build")

DEFAULT_WEB = {"service": "web", "port": 80}

# Registry host per `registry` slot implementation. `oci` (and anything unknown)
# must set `url` in config/providers/registry_<impl>.yaml or the providers block.
REGISTRY_URLS = {
    "ghcr": "ghcr.io",
    "dockerhub": "docker.io",
    "quay": "quay.io",
}


class RenderError(RuntimeError):
    """Raised when an app manifest cannot be resolved in strict mode."""


def _display(path: pathlib.Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_yaml(path: pathlib.Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def write_text(path: pathlib.Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")
    os.chmod(path, 0o600)


def gh_api_raw(endpoint: str) -> str:
    """Read a repo file through the GitHub REST API (stdlib, no `gh`)."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        raise RenderError("GH_TOKEN is not set (needed to fetch app manifests)")
    request = urllib.request.Request(
        f"https://api.github.com/{endpoint}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise RenderError(f"GitHub API returned HTTP {exc.code} for {endpoint}") from exc
    except urllib.error.URLError as exc:
        raise RenderError(f"could not reach the GitHub API: {exc.reason}") from exc


def local_app_dir(repo: str) -> pathlib.Path | None:
    """A local checkout standing in for an app repo, from ``SEALEDSTACK_LOCAL_APPS``.

    ``SEALEDSTACK_LOCAL_APPS="owner/repo=/path/to/checkout,other/repo=/path2"``.
    Lets CI and developers validate an app against an infra config without GitHub
    access (the reference-app job uses it); nothing in production sets it.
    """
    for item in os.environ.get("SEALEDSTACK_LOCAL_APPS", "").split(","):
        name, _, directory = item.strip().partition("=")
        if name == repo and directory:
            return pathlib.Path(directory)
    return None


def fetch_repo_file(repo: str, ref: str, path: str) -> str:
    local = local_app_dir(repo)
    if local is not None:
        target = local / path
        if not target.is_file():
            raise RenderError(f"{repo}: {path} not found in the local checkout {local}")
        return target.read_text(encoding="utf-8")
    endpoint = f"repos/{repo}/contents/{path}?ref={ref}"
    return gh_api_raw(endpoint)


def fetch_app_manifest(repo: str, ref: str) -> dict:
    return yaml.safe_load(fetch_repo_file(repo, ref, "sealedstack.yaml")) or {}


def synthesize_manifest(name: str) -> dict:
    return {
        "name": name,
        "web": dict(DEFAULT_WEB),
        "needs": {},
        "secrets": [],
    }


def trigger_hash(manifest: dict, compose: dict | None, source: dict) -> str:
    """Stable hash of the deploy triggers (H11): manifest, compose, source ref."""
    payload = json.dumps(
        {"manifest": manifest, "compose": compose, "source": source},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def repo_slug(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def resolve_registry(stack: dict) -> dict:
    """The rendered `registry` slot config (spec 4.5, D-033).

    The file ``config/providers/registry_<impl>.yaml`` wins over the stack.yaml
    ``providers`` block. ``owner`` is the registry namespace; for ghcr the pull
    login is the same user.
    """
    impl = (stack.get("slots") or {}).get("registry") or "ghcr"
    file_cfg = {}
    file = CONFIG / "providers" / f"registry_{impl}.yaml"
    if file.exists():
        file_cfg = load_yaml(file)
    block = (stack.get("providers") or {}).get(f"registry_{impl}") or {}
    merged = {**block, **file_cfg}

    owner = merged.get("owner") or merged.get("namespace") or merged.get("username") or ""
    url = merged.get("url") or REGISTRY_URLS.get(impl, "")
    image_prefix = f"{url}/{owner}" if url and owner else ""
    return {
        "impl": impl,
        "url": url,
        "owner": owner,
        "username": merged.get("username") or owner,
        "image_prefix": image_prefix,
    }


def resolve_manifest(name: str, entry: dict, *, allow_unresolved: bool) -> tuple[dict, dict]:
    """Return (manifest, flags). ``flags`` records how the manifest was obtained."""
    source = entry.get("source", {})
    if "image" in source:
        return synthesize_manifest(name), {"synthesized": True, "unresolved": False}
    try:
        manifest = fetch_app_manifest(source["repo"], source["ref"])
        return manifest, {"synthesized": False, "unresolved": False}
    except Exception as exc:  # noqa: BLE001 - surface any gh/network failure uniformly
        if not allow_unresolved:
            raise RenderError(f"{name}: could not fetch manifest: {exc}") from exc
        return {}, {"synthesized": False, "unresolved": True, "reason": str(exc)}


def render_deploy_compose(
    source: dict,
    manifest: dict,
    compose: dict,
    registry: dict,
) -> tuple[str, list[str]]:
    """The `ci`-mode deploy compose (build: stripped) and its lint problems."""
    mode = (manifest.get("build") or {}).get("mode") or "ci"
    slug = repo_slug(source.get("repo", ""))
    problems = lint_compose.lint_build(
        compose,
        mode,
        registry=registry.get("url", ""),
        owner=registry.get("owner", ""),
        repo=slug,
    )
    rendered = yaml.safe_dump(
        lint_compose.attach_platform_network(lint_compose.strip_build(compose), manifest), sort_keys=False
    )
    return rendered, problems


def build_app(
    name: str,
    entry: dict,
    *,
    allow_unresolved: bool,
    registry: dict,
) -> dict:
    manifest, flags = resolve_manifest(name, entry, allow_unresolved=allow_unresolved)
    source = entry.get("source", {})
    build = manifest.get("build") or {}
    mode = build.get("mode") or "ci"

    rendered = {
        "name": manifest.get("name", name),
        "source": source,
        "servers": entry.get("servers", []),
        "hostnames": entry.get("hostnames", []),
        "redirects": entry.get("redirects", {}),
        "access": entry.get("access", "public"),
        "user_auth": entry.get("user_auth"),
        "web": manifest.get("web", dict(DEFAULT_WEB)),
        "needs": manifest.get("needs", {}),
        "secrets": manifest.get("secrets", []),
        "manifest": manifest,
        "_synthesized": flags["synthesized"],
        "_unresolved": flags["unresolved"],
        "_build_mode": mode,
        "_deploy_compose": "",
        "_build_problems": [],
        "_security_problems": [],
    }
    if flags["unresolved"]:
        rendered["_reason"] = flags["reason"]

    compose = None
    compose_path = manifest.get("compose")
    if not flags["unresolved"] and compose_path and "repo" in source:
        try:
            compose = yaml.safe_load(fetch_repo_file(source["repo"], source["ref"], compose_path)) or {}
        except Exception as exc:  # noqa: BLE001
            flags["compose_error"] = str(exc)
            rendered["_compose_error"] = str(exc)
    rendered["_compose"] = compose
    # D-063: a compose file that asks Docker for the host is refused, never deployed.
    rendered["_security_problems"] = lint_compose.lint_security(compose)
    rendered["_compose_hash"] = trigger_hash(manifest, compose, source)

    # ci mode: render the deploy compose Dokploy pulls (spec 4.2, 4.5).
    if mode == "ci" and "repo" in source:
        if compose is None:
            rendered["_build_problems"].append(
                "build.mode is 'ci' but the compose could not be fetched; "
                "Dokploy would receive no deploy compose"
            )
        else:
            if not (registry.get("url") and registry.get("owner")):
                rendered["_build_problems"].append(
                    "build.mode is 'ci' but the registry slot has no url/owner; set "
                    "slots.registry and providers.registry_<impl>.owner"
                )
            deploy_compose, problems = render_deploy_compose(source, manifest, compose, registry)
            rendered["_deploy_compose"] = deploy_compose
            rendered["_build_problems"].extend(problems)

    return rendered


def render_all(*, allow_unresolved: bool = False) -> dict:
    stack = load_yaml(CONFIG / "stack.yaml")
    servers = load_yaml(CONFIG / "servers.yaml")
    apps = load_yaml(CONFIG / "apps.yaml")
    registry = resolve_registry(stack)

    rendered_apps: dict[str, dict] = {}
    disabled: list[str] = []
    unresolved: list[str] = []

    # Resolve everything before writing, so a strict failure never leaves a
    # half-written .build that plan/apply could act on.
    for name, entry in (apps.get("apps") or {}).items():
        if entry.get("enabled") is False:
            disabled.append(name)
            continue
        rendered = build_app(
            name, entry, allow_unresolved=allow_unresolved, registry=registry
        )
        rendered["_disabled"] = False
        if rendered["_unresolved"]:
            unresolved.append(name)
        rendered_apps[name] = rendered

    unsafe = [
        f"app '{name}': {problem}"
        for name, rendered in rendered_apps.items()
        for problem in rendered.get("_security_problems") or []
    ]
    if unsafe:
        raise RenderError(
            "the compose file asks for host access (refused, D-063):\n  " + "\n  ".join(unsafe)
        )

    if unresolved and not allow_unresolved:
        raise RenderError(
            "unresolved app(s): "
            + ", ".join(unresolved)
            + " — run inside the toolbox with GitHub access, or set `enabled: false` to pause"
        )

    for name, rendered in rendered_apps.items():
        payload = {k: v for k, v in rendered.items() if k != "_compose"}
        write_json(BUILD / "apps" / f"{name}.json", payload)
        deploy = rendered.get("_deploy_compose")
        deploy_path = BUILD / "apps" / f"{name}.deploy-compose.yml"
        if deploy:
            write_text(deploy_path, deploy)
        elif deploy_path.exists():
            deploy_path.unlink()

    # A paused app must not leave a stale rendered file that plan/apply would
    # keep deploying (C2).
    for name in disabled:
        for stale in (
            BUILD / "apps" / f"{name}.json",
            BUILD / "apps" / f"{name}.deploy-compose.yml",
        ):
            if stale.exists():
                stale.unlink()

    write_json(BUILD / "stack.json", stack)
    write_json(BUILD / "servers.json", servers)
    write_json(BUILD / "registry.json", registry)

    for name in disabled:
        rendered_apps[name] = {"name": name, "_disabled": True, "_unresolved": False}

    return rendered_apps


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-unresolved",
        action="store_true",
        help="tolerate an app manifest that cannot be fetched (offline/static use)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help=argparse.SUPPRESS,  # strict is now the default; kept for compatibility
    )
    args = parser.parse_args()

    os.umask(0o077)

    try:
        rendered = render_all(allow_unresolved=args.allow_unresolved)
    except RenderError as exc:
        print(f"render: {exc}", file=sys.stderr)
        return 1

    for name, app in rendered.items():
        if app.get("_disabled"):
            print(f"  {name}: disabled (enabled: false)")
        elif app["_unresolved"]:
            print(f"  {name}: unresolved ({app.get('_reason', '')})")
        elif app["_synthesized"]:
            print(f"  {name}: synthesized from image source")
        else:
            mode = app.get("_build_mode", "ci")
            extra = " (+ deploy compose)" if app.get("_deploy_compose") else ""
            print(f"  {name}: rendered (build.mode: {mode}{extra})")

    print(f"render: wrote {len([a for a in rendered.values() if not a.get('_disabled')])} app(s) to {_display(BUILD)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
