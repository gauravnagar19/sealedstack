"""``app-secrets``: give every app its own Dokploy secrets provider (D-088).

Runs after ``platform apply`` and ``connector`` (the Dokploy project must exist to be assigned) and before
``post-apply`` (whose ``env-sync`` writes the ``${{vault.<provider>.KEY}}`` references).
For each rendered app whose ``sealedstack.yaml: secrets`` is the ``{provider, keys}`` form:

1. A provider with that name exists: make sure it is assigned to the app's project. The
   update sends the masked config back, which Dokploy keeps as stored, so no token is
   needed for this step.
2. It does not exist and ``DOPPLER_ADMIN_TOKEN`` is in the infra secret source: create the
   Doppler project ``<provider>`` (Doppler adds the ``dev``/``stg``/``prd`` configs), mint a
   read-only service token for ``<provider>/prd``, and create the Dokploy provider with it,
   assigned to the project. The minted token goes straight from Doppler to Dokploy; it is
   never printed or stored anywhere else.
3. It does not exist and there is no admin token: print the steps to do it by hand and go
   on (``env-sync`` then stops with the same instructions).

With the admin token, keys the manifest lists but the Doppler config does not have are
named (names only, never values) so the owner knows what to fill in before the deploy.

``DOPPLER_ADMIN_TOKEN`` is a Doppler personal token (``dp.pt.``) or a service-account token
(``dp.sa.``, a paid Doppler feature). It can create projects and tokens in the whole
workplace, so it is read only here, only in this approved apply phase, and is optional.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import platformstate as platformstate_mod
from . import secrets as secrets_mod
from .http import HttpClient, UrllibClient, one_line

API = "https://api.doppler.com"
ADMIN_TOKEN = "DOPPLER_ADMIN_TOKEN"
ADMIN_PREFIXES = ("dp.pt.", "dp.sa.")  # personal token, service-account token
CONFIG = "prd"
DOCS = "https://docs.doppler.com/docs/personal-tokens"
MASK = "********"  # what Dokploy shows instead of a stored token, and keeps on update


class AppSecretsError(RuntimeError):
    """Creating or assigning a provider failed."""


class DopplerAdmin:
    """The few Doppler calls that need a workplace-level token. The token is only ever in a header."""

    def __init__(self, token: str, *, http: Optional[HttpClient] = None, api: str = API) -> None:
        if not token.startswith(ADMIN_PREFIXES):
            raise AppSecretsError(
                f"{ADMIN_TOKEN} must be a Doppler personal token (dp.pt.) or service-account token (dp.sa.); "
                f"a service token (dp.st.) cannot create projects or tokens ({DOCS})"
            )
        self.token = token
        self.http = http or UrllibClient()
        self.api = api.rstrip("/")

    def _call(self, method: str, path: str, body=None, *, ok_missing: bool = False):
        response = self.http.request(
            method,
            f"{self.api}{path}",
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            json_body=body,
        )
        if ok_missing and response.status == 404:
            return None
        if response.status >= 400:
            said = ""
            try:
                messages = (response.json() or {}).get("messages") or []
                said = f": {one_line(messages[0])}" if messages else ""
            except Exception:  # noqa: BLE001 - not JSON
                pass
            raise AppSecretsError(f"Doppler {method} {path.split('?')[0]} failed (HTTP {response.status}){said}")
        return response.json() or {}

    def project_exists(self, project: str) -> bool:
        return self._call("GET", f"/v3/projects/project?project={project}", ok_missing=True) is not None

    def create_project(self, project: str) -> None:
        self._call("POST", "/v3/projects", {"name": project, "description": "sealedstack app secrets"})

    def secret_names(self, project: str, config: str) -> list[str]:
        data = self._call("GET", f"/v3/configs/config/secrets/names?project={project}&config={config}")
        return [str(name) for name in (data.get("names") or [])]

    def mint_read_token(self, project: str, config: str, name: str) -> str:
        data = self._call(
            "POST", "/v3/configs/config/tokens", {"project": project, "config": config, "name": name, "access": "read"}
        )
        key = str((data.get("token") or {}).get("key") or "")
        if not key:
            raise AppSecretsError(f"Doppler minted no token for {project}/{config}")
        return key


def manual_steps(app: str, provider: str) -> str:
    return (
        f"{app}: Dokploy has no secrets provider '{provider}'. Either add {ADMIN_TOKEN} (a Doppler personal token) "
        f"to the infra secret source and run apply again, or by hand: in Doppler create project '{provider}', open "
        f"its '{CONFIG}' config, create a read-only service token; in Dokploy open Settings, Secrets providers, "
        f"create a Doppler provider named '{provider}' with that token and assign it to the '{app}' project."
    )


def _assigned(provider: dict, project_id: str) -> bool:
    return any(str((a or {}).get("projectId")) == project_id for a in provider.get("assignments") or [])


def _ensure_assigned(client, provider: dict, project_id: str) -> bool:
    """Add the project to the provider's assignments; ``True`` when it changed."""
    if _assigned(provider, project_id):
        return False
    assignments = [a for a in provider.get("assignments") or [] if isinstance(a, dict)]
    assignments.append({"projectId": project_id, "environmentIds": []})
    config = dict(provider.get("config") or {})
    config.setdefault("providerType", provider.get("providerType"))
    client.post(
        "/api/vaultProvider.update",
        {"vaultProviderId": provider["vaultProviderId"], "name": provider["name"], "config": config, "assignments": assignments},
    )
    return True


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    client=None,
    state=None,
    source: Optional[secrets_mod.SecretSource] = None,
    doppler: Optional[DopplerAdmin] = None,
    http: Optional[HttpClient] = None,
    out=None,
) -> None:
    env = dict(env or {})
    out = out if out is not None else sys.stdout
    apps_dir = Path(config.code_root) / ".build" / "apps"
    wanted = []
    for path in sorted(apps_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        vault = data.get("vault") or {}
        if vault.get("provider"):
            wanted.append((str(data.get("name") or path.stem), str(vault["provider"]), [str(k) for k in vault.get("keys") or []]))
    if not wanted:
        print("app-secrets: no app uses its own secrets provider; nothing to do", file=out)
        return

    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    state = state or platformstate_mod.PlatformState(config, env, source)
    client = client or dokploy_mod.from_config(config, env, source)
    projects = state.output("projects") or {}
    if doppler is None:
        token = env.get(ADMIN_TOKEN) or secrets_mod.lookup(source, config, ADMIN_TOKEN).value or ""
        doppler = DopplerAdmin(token.strip(), http=http) if token.strip() else None
    providers = {str(p.get("name")): p for p in client.vault_providers() or [] if isinstance(p, dict)}
    token_name = f"sealedstack-dokploy-{config.name}"

    for app, provider_name, keys in wanted:
        project_id = str(projects.get(app) or "")
        if not project_id:
            print(f"==> {app}: no Dokploy project yet; skipping", file=out)
            continue
        print(f"==> {app}: secrets provider '{provider_name}'", file=out)
        provider = providers.get(provider_name)
        if provider is not None:
            changed = _ensure_assigned(client, provider, project_id)
            print(f"    {'assigned to' if changed else 'already assigned to'} project {app}", file=out)
        elif doppler is None:
            print(f"    warn: {manual_steps(app, provider_name)}", file=out)
            continue
        else:
            if not doppler.project_exists(provider_name):
                doppler.create_project(provider_name)
                print(f"    created Doppler project '{provider_name}'", file=out)
            service_token = doppler.mint_read_token(provider_name, CONFIG, token_name)
            client.post(
                "/api/vaultProvider.create",
                {
                    "name": provider_name,
                    "config": {"providerType": "doppler", "serviceToken": service_token},
                    "assignments": [{"projectId": project_id, "environmentIds": []}],
                },
            )
            print(f"    created Dokploy provider '{provider_name}' (read-only token for {provider_name}/{CONFIG}) and assigned it", file=out)
        if doppler is not None and keys:
            present = set(doppler.secret_names(provider_name, CONFIG))
            missing = [k for k in keys if k not in present]
            if missing:
                print(
                    f"    warn: add these keys in Doppler {provider_name}/{CONFIG} before the deploy "
                    f"(a missing one fails it): {', '.join(missing)}",
                    file=out,
                )
    print("app-secrets: done", file=out)


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        run(config, env)
    except (config_mod.ConfigError, dokploy_mod.DokployError, AppSecretsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
