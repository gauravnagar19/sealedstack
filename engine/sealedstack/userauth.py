"""``user-auth`` in Python (registry slot ``user_auth``, D-047).

Config-driven auth providers (oidc, zitadel, logto, authentik, keycloak,
supabase, firebase, workos, auth0) need no server-side component. For each app
with ``user_auth:`` set this:

1. resolves the provider (``apps.<app>.user_auth`` -> ``slots.user_auth``);
2. reads non-secret config from ``config/providers/user_auth_<id>.yaml`` (or the
   ``providers:`` block of ``stack.yaml``) and secrets from the app-secret
   project (``prod.<app>.<KEY>``);
3. merges the provider's env keys into ``.build/env.json``; and
4. prints the callback URLs and webhook paths to register at the provider.

Replaces ``scripts/user-auth.sh``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import envfile
from . import secrets as secrets_mod

# provider -> (ENV_NAME, source) where source is ("CONFIG", key) | ("SECRET", key).
# Keep in sync with registry/user_auth.yaml.
PROVIDER_ENV_KEYS: dict[str, list[tuple[str, str, str]]] = {
    "oidc": [
        ("OIDC_ISSUER", "CONFIG", "issuer"),
        ("OIDC_CLIENT_ID", "CONFIG", "client_id"),
        ("OIDC_CLIENT_SECRET", "SECRET", "OIDC_CLIENT_SECRET"),
    ],
    "better_auth": [
        ("BETTER_AUTH_URL", "CONFIG", "url"),
        ("BETTER_AUTH_SECRET", "SECRET", "BETTER_AUTH_SECRET"),
    ],
    "zitadel": [
        ("ZITADEL_ISSUER", "CONFIG", "issuer"),
        ("ZITADEL_CLIENT_ID", "CONFIG", "client_id"),
        ("ZITADEL_CLIENT_SECRET", "SECRET", "ZITADEL_CLIENT_SECRET"),
    ],
    "logto": [
        ("LOGTO_ISSUER", "CONFIG", "issuer"),
        ("LOGTO_CLIENT_ID", "CONFIG", "client_id"),
        ("LOGTO_CLIENT_SECRET", "SECRET", "LOGTO_CLIENT_SECRET"),
    ],
    "authentik": [
        ("AUTHENTIK_ISSUER", "CONFIG", "issuer"),
        ("AUTHENTIK_CLIENT_ID", "CONFIG", "client_id"),
        ("AUTHENTIK_CLIENT_SECRET", "SECRET", "AUTHENTIK_CLIENT_SECRET"),
    ],
    "keycloak": [
        ("KEYCLOAK_ISSUER", "CONFIG", "issuer"),
        ("KEYCLOAK_CLIENT_ID", "CONFIG", "client_id"),
        ("KEYCLOAK_CLIENT_SECRET", "SECRET", "KEYCLOAK_CLIENT_SECRET"),
    ],
    "supabase": [
        ("SUPABASE_URL", "CONFIG", "project_url"),
        ("SUPABASE_ANON_KEY", "CONFIG", "anon_key"),
        ("SUPABASE_SERVICE_ROLE_KEY", "SECRET", "SUPABASE_SERVICE_ROLE_KEY"),
    ],
    "firebase": [
        ("FIREBASE_PROJECT_ID", "CONFIG", "project_id"),
        ("FIREBASE_SERVICE_ACCOUNT", "SECRET", "FIREBASE_SERVICE_ACCOUNT"),
    ],
    "workos": [
        ("WORKOS_CLIENT_ID", "CONFIG", "client_id"),
        ("WORKOS_REDIRECT_URI", "CONFIG", "redirect_uri"),
        ("WORKOS_API_KEY", "SECRET", "WORKOS_API_KEY"),
    ],
    "auth0": [
        ("AUTH0_DOMAIN", "CONFIG", "domain"),
        ("AUTH0_CLIENT_ID", "CONFIG", "client_id"),
        ("AUTH0_CLIENT_SECRET", "SECRET", "AUTH0_CLIENT_SECRET"),
    ],
}


def resolve_impl(config: config_mod.Config, app: str) -> str:
    entry = (config.apps.get("apps") or {}).get(app) or {}
    value = entry.get("user_auth")
    if not value or value is True:
        value = (config.stack.get("slots") or {}).get("user_auth")
    return "" if value in (None, True) else str(value)


def provider_cfg(config: config_mod.Config, impl: str, key: str) -> str:
    """File config (``providers/user_auth_<impl>.yaml``) wins over the stack block."""
    value = None
    file = config.config_dir / "providers" / f"user_auth_{impl}.yaml"
    if file.exists():
        data = config_mod.load_yaml(file)
        value = data.get(key)
    if not value:
        block = (config.stack.get("providers") or {}).get(f"user_auth_{impl}") or {}
        value = block.get(key)
    return "" if value is None else str(value)


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    apps: Optional[list[str]] = None,
    source: Optional[secrets_mod.SecretSource] = None,
    project: Optional[str] = None,
    out=None,
) -> dict:
    """Merge user-auth env into ``.build/env.json``; return the env map."""
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    build_dir = Path(config.code_root) / ".build"
    if not (build_dir / "stack.json").exists():
        raise FileNotFoundError("user-auth: run `sealedstack debug render` first (missing .build/stack.json)")

    path = envfile.env_path(config.code_root)
    env_map = envfile.load(path)

    if apps is None:
        apps = [
            name
            for name, entry in (config.apps.get("apps") or {}).items()
            if (entry or {}).get("user_auth") is not None
        ]
    if not apps:
        print("user-auth: no apps declare user_auth; nothing to do", file=out)
        return env_map

    source = source or secrets_mod.build_source(config.stack, config.config_dir, env, project=project or config.app_project())

    for app in apps:
        impl = resolve_impl(config, app)
        if not impl:
            print(
                f"user-auth: {app}: no user_auth implementation "
                "(set apps.<app>.user_auth or slots.user_auth)",
                file=out,
            )
            continue
        spec = PROVIDER_ENV_KEYS.get(impl)
        if not spec:
            print(
                f"user-auth: {app}: '{impl}' has no adapter mapping "
                "(native/clerk providers use the secrets slot directly)",
                file=out,
            )
            continue

        values: dict[str, str] = {}
        for env_name, kind, key in spec:
            if kind == "CONFIG":
                value = provider_cfg(config, impl, key)
            else:
                source_key = config.app_secret_key(app, key)
                result = source.get(source_key)
                value = result.value if result.found else ""
            if not value:
                print(f"user-auth: {app}: warning: {env_name} is empty [{kind}:{key}]", file=out)
            values[env_name] = value

        envfile.merge_app(env_map, app, values)
        print(f"==> {app} (user_auth: {impl})", file=out)
        print(f"    injected env keys into .build/env.json: {', '.join(sorted(values))}", file=out)

        app_json = build_dir / "apps" / f"{app}.json"
        if app_json.exists():
            data = json.loads(app_json.read_text(encoding="utf-8") or "{}")
            for hostname in data.get("hostnames") or []:
                print(f"    register callback/redirect: https://{hostname}/auth/callback", file=out)
            manifest = data.get("manifest") or {}
            for webhook in ((manifest.get("user_auth") or {}).get("webhook_paths") or []):
                print(f"    webhook (gate-exempt):      {webhook}", file=out)

    envfile.save(path, env_map)
    print(f"user-auth: wrote {path}", file=out)
    return env_map


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    apps = list(argv) if argv else None
    try:
        run(config, env, apps=apps)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
