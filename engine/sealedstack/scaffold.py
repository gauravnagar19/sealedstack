"""The config that ``setup`` / ``debug init`` write (UX §4 step 3b, D-050).

Pure functions: they return plain dicts, so the Setup job, the legacy wizard and
the tests all produce -- and check -- the same files. Everything a user might want
to rename is written out explicitly: the secret key names (``secrets.keys``) and,
for every existing machine, ``bootstrap.ip_secret`` / ``password_secret``.
Config holds names only, never secret values or IP addresses (D-008, D-049).
"""
from __future__ import annotations

import json
from typing import Iterable, Mapping, Optional

from . import config as config_mod

# Providers for machines that already exist: their first login (public IP and
# root password) comes from the secret source (D-049).
EXISTING_MACHINE_PROVIDERS = ("any-ssh", "netcup", "contabo")


def server_entry(name: str, provider: str, role: str = "control", os_name: str = "ubuntu-24.04") -> dict:
    """One ``servers.yaml`` entry; existing machines get an explicit bootstrap block."""
    entry: dict = {"provider": provider, "role": role, "os": os_name}
    if provider in EXISTING_MACHINE_PROVIDERS:
        ip_secret, password_secret = config_mod.default_bootstrap_secrets(name)
        entry["bootstrap"] = {
            "method": "password",
            "ip_secret": ip_secret,
            "password_secret": password_secret,
        }
    return entry


def servers_config(servers: Iterable[tuple[str, str, str, str]]) -> dict:
    """``[(name, provider, role, os)]`` -> the ``servers.yaml`` document."""
    return {"servers": {name: server_entry(name, provider, role, os_name) for name, provider, role, os_name in servers}}


def stack_config(
    *,
    infra_name: str,
    domain: str,
    owner_email: str,
    admin_user: str = "admin",
    ssh_keys: Optional[list[str]] = None,
    approvers: Optional[list[str]] = None,
    account_id: str = "",
    team_name: str = "",
    tailnet: str = "",
    secrets_source: str = "bitwarden",
    ci: str = "",
    forge: str = "",
) -> dict:
    stack: dict = {
        "name": infra_name,
        "domain": domain,
        "owner_email": owner_email,
        "admin_user": admin_user,
        "admin": {"ssh_keys": list(ssh_keys or [])},
        # D-052: who may approve write commands in CI. Defaults to the owner.
        "approvers": list(approvers or []),
        "slots": {
            "compute": "any-ssh",
            "mesh": "tailscale",
            "ingress": "cloudflare",
            "gate": "cloudflare_access",
            "secrets": secrets_source,
            "backup": "r2",
            "platform": "dokploy",
        },
        "cloudflare": {"account_id": account_id, "zone": domain, "team_name": team_name or domain.split(".")[0]},
        "mesh": {"tailnet": tailnet, "policy": "managed"},
        "gate": {"policies": {"owner-only": {"emails": [owner_email], "session": "24h"}}, "catch_all": True},
        "edge_security": {
            "bot_fight_mode": True,
            "block_ai_bots": True,
            "blocked_paths": ["/.env", "/.git", "xmlrpc.php", "/wp-", "*.php"],
        },
        "state": {"backend": "r2", "bucket": "sealedstack-state"},
        # D-050: every secret's key name, written out so it can be reviewed and
        # renamed. `project` / `app_project` are the secret-source projects.
        "secrets": {
            "project": config_mod.DEFAULT_SECRET_PROJECT,
            "app_project": config_mod.DEFAULT_APP_SECRET_PROJECT,
            "keys": config_mod.default_secret_keys(),
        },
    }
    if secrets_source == "infisical":
        # Where the secrets live is plain config; the owner fills in the project ID (D-067).
        stack["providers"] = {
            "secrets_infisical": {"project_id": "", "environment": "prod", "secret_path": "/"},
        }
    if ci:
        stack["slots"]["ci"] = ci
    if forge:
        stack["slots"]["forge"] = forge
    return stack


def apps_config(first_server: str, domain: str) -> dict:
    """The first app: ``hello`` (``traefik/whoami``, owner-only)."""
    return {
        "apps": {
            "hello": {
                "source": {"image": "traefik/whoami:v1.10.3"},
                "servers": [first_server],
                "hostnames": [f"hello.{domain}"],
                "access": "owner-only",
            }
        }
    }


def gitignore() -> str:
    return ".sealedstack/\n*.tfstate*\n"


# The repository secrets each secret source needs (D-066, D-067); Bitwarden's is the default.
SOURCE_TOKEN_VARS = {
    "doppler": ["DOPPLER_TOKEN", "DOPPLER_TOKEN_APPS"],
    "infisical": ["INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET", "INFISICAL_TOKEN"],
}


def devcontainer_json(image: str, secrets_source: str = "bitwarden") -> str:
    """A devcontainer that runs the pinned engine image (D-046).

    ``image`` includes the tag (``ghcr.io/<owner>/sealedstack:<version>``). Local
    use is optional and read-mostly; this gives a browser Codespace, or a local VS
    Code with a container runtime, a terminal with `sealedstack` ready and no
    toolchain installed on the host.

    The remote environment passes through the token of the chosen secret source (the
    only secrets the source needs); Bitwarden's unless ``secrets_source`` is ``doppler`` or
    ``infisical``.
    """
    token_vars = SOURCE_TOKEN_VARS.get(secrets_source, ["BWS_ACCESS_TOKEN"])
    remote_env = {name: "${localEnv:" + name + "}" for name in token_vars}
    remote_env["GH_TOKEN"] = "${localEnv:GH_TOKEN}"
    return json.dumps(
        {
            "name": "sealedstack",
            "image": image,
            "workspaceFolder": "/infra",
            "workspaceMount": "source=${localWorkspaceFolder},target=/infra,type=bind",
            "containerEnv": {
                "SEALEDSTACK_CONFIG": "/infra/config",
                "SEALEDSTACK_WORKDIR": "/infra/.sealedstack",
            },
            "remoteEnv": remote_env,
            "customizations": {"vscode": {"extensions": []}},
        },
        indent=2,
    ) + "\n"


def setup_readme(infra_name: str, image: str, version: str) -> str:
    return f"""# {infra_name}

Configuration for a sealed origin, run by sealedstack {version}.
This repository holds only configuration and a pinned tool version: no secrets, no
IP addresses and no sealedstack source code.

## How changes happen

Every change is a pull request. A plan appears as a comment on the PR; after you
merge, an approver comments `/sealedstack approve` (or you run the workflow) and
the change is applied. The pinned **Status** issue shows health, versions and drift.

| You want to | Do this |
|---|---|
| Check prerequisites | Comment `/sealedstack check` on a pull request |
| Add a server or an app | Actions, sealedstack, Run workflow: `add-server` or `add-app` |
| Bring everything up | Actions, sealedstack, Run workflow: `up` |
| Upgrade sealedstack | Merge the version-bump pull request (it changes `sealedstack.version` and the workflow ref together) |

## Files

- `config/stack.yaml`, `servers.yaml`, `apps.yaml`: what you edit, in pull requests.
- `config/tailscale-policy.hujson`: your tailnet policy (imported by `check`); sealedstack adds only its own block.
- `sealedstack.version` and `.github/workflows/sealedstack.yml`: the pinned version (they must match).
- `.devcontainer/devcontainer.json`: opens a Codespace with the pinned image (`{image}`) for read-only commands.

Secrets are never stored here. Their **names** are in `config/stack.yaml` under
`secrets.keys` and in `config/servers.yaml` under `bootstrap`; the values live in
your secret source.
"""
