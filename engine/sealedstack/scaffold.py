"""The config that ``setup`` / ``debug init`` write (UX §4 step 3b, D-050).

Pure functions: they return plain dicts, so the Setup job, the legacy wizard and
the tests all produce -- and check -- the same files. Everything a user might want
to rename is written out explicitly: the secret key names (``secrets.keys``) and,
for every existing machine, ``bootstrap.ip_secret`` / ``password_secret``.
Config holds names only, never secret values or IP addresses (D-008, D-049).
"""
from __future__ import annotations

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
