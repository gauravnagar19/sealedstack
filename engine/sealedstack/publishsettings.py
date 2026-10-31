"""``publish-app-settings`` (spec 6.5, D-047).

Write the deploy settings app CI needs into each app repo's GitHub variables and
the Dokploy API key as a repository secret, through the ``forge`` abstraction (M7).
Replaces ``scripts/publish-app-settings``; no longer shells out to ``gh``.

The app repo's CI never names the infra repo: it reads only these values (D-023).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import dokploymembers as dokploymembers_mod
from . import forge as forge_mod
from . import platformstate as platformstate_mod
from . import secrets as secrets_mod


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    apps: Optional[list[str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    state: Optional[platformstate_mod.PlatformState] = None,
    forge: Optional[forge_mod.Forge] = None,
    generated=None,
    http=None,
    out=None,
) -> None:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    forge = forge or forge_mod.GitHubForge.from_env(env)

    entries = config.apps.get("apps") or {}
    repo_apps = [name for name, entry in entries.items() if (entry or {}).get("source", {}).get("repo")]
    if apps:
        repo_apps = [name for name in repo_apps if name in apps]
        if not repo_apps:
            print("publish-app-settings: no matching repo app", file=sys.stderr)
            raise config_mod.ConfigError("no matching repo app")
    if not repo_apps:
        print("publish-app-settings: no repo apps; nothing to publish", file=out)
        return

    compose_ids = (state or platformstate_mod.PlatformState(config, env, source)).output("compose_ids") or {}
    dokploy_url = f"http://{dokploy_mod.control_host(config)}:3000"
    from . import generated as generated_mod

    # The admin key is used here only to provision the scoped members (D-063); it is
    # never published. Each app repo gets its own member's key.
    admin_key = generated_mod.resolve_api_key(config, env, source, generated)
    if not admin_key:
        raise config_mod.ConfigError(dokploy_mod.NO_KEY_MESSAGE)
    generated = generated or generated_mod.shared(config, env, source, http=http)
    member_keys = dokploymembers_mod.ensure(
        config,
        {app: compose_ids.get(app) or {} for app in repo_apps},
        admin_key=admin_key,
        generated=generated,
        http=http,
        out=out,
        everything=compose_ids,
    )

    providers = config.stack.get("providers") or {}
    mesh = providers.get("mesh_tailscale") or {}
    # D-057: app release jobs join as tag:release with their own OIDC trust
    # credential. The infra repo's tag:ci credential (oauth_client_id/audience) is
    # never published to app repos.
    ts_client_id = str(mesh.get("release_oauth_client_id") or env.get("TS_RELEASE_OAUTH_CLIENT_ID") or "")
    ts_audience = str(mesh.get("release_audience") or env.get("TS_RELEASE_OAUTH_AUDIENCE") or "")

    registry = ""
    registry_file = Path(config.code_root) / ".build" / "registry.json"
    if registry_file.exists():
        try:
            registry = str((json.loads(registry_file.read_text(encoding="utf-8") or "{}")).get("image_prefix") or "")
        except json.JSONDecodeError:
            registry = ""

    for app in repo_apps:
        repo = str((entries.get(app) or {}).get("source", {}).get("repo") or "")
        print(f"==> {app} ({repo})", file=out)
        app_ids = json.dumps(compose_ids.get(app) or {}, separators=(",", ":"))
        _set_variable(forge, repo, "SEALEDSTACK_COMPOSE_IDS", app_ids, out)
        _set_variable(forge, repo, "SEALEDSTACK_DOKPLOY_URL", dokploy_url, out)
        _set_variable(forge, repo, "SEALEDSTACK_TS_RELEASE_CLIENT_ID", ts_client_id, out)
        _set_variable(forge, repo, "SEALEDSTACK_TS_RELEASE_AUDIENCE", ts_audience, out)
        _set_variable(forge, repo, "SEALEDSTACK_REGISTRY", registry, out)

        # FR-2: always a repository secret. The release job declares no
        # `environment:`, so an environment secret would be invisible to it, and
        # private repos on GitHub Free have no environments at all.
        has_environments = forge.ensure_environment(repo, "production")
        if app not in member_keys:
            continue
        forge.set_secret(repo, "SEALEDSTACK_DOKPLOY_API_KEY", member_keys[app])
        note = (
            "environments are available, but the release job reads a repository secret"
            if has_environments
            else "environments are not available for this repository (private repo on GitHub Free, or the token cannot create them)"
        )
        print(f"    secret SEALEDSTACK_DOKPLOY_API_KEY (the scoped key of this app's Dokploy member; mode: repository secret; {note})", file=out)

    print("publish-app-settings: done", file=out)


def _set_variable(forge: forge_mod.Forge, repo: str, name: str, value: str, out) -> None:
    if not value:
        print(f"    skip {name} (empty)", file=out)
        return
    forge.set_variable(repo, name, value)
    print(f"    variable {name}", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    apps = list(argv if argv is not None else sys.argv[1:]) or None
    config = config_mod.load_config(env=env)
    try:
        run(config, env, apps)
    except (config_mod.ConfigError, forge_mod.ForgeError) as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
