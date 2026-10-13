"""``platform`` in Python (spec sections 9 and 11, D-047).

Plan or apply the platform stack. Replaces ``scripts/platform.sh``. Like the edge
phase, secrets become ``TF_VAR_*`` for the child ``tofu``; a missing required
secret aborts before any change.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import r2 as r2_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod


def control_server(config: config_mod.Config) -> str:
    """The server whose ``role`` is ``control`` (its Dokploy host)."""
    servers = config.servers.get("servers") or {}
    for name, spec in servers.items():
        if (spec or {}).get("role") == "control":
            return str(name)
    return ""


def control_host(config: config_mod.Config) -> str:
    name = control_server(config)
    suffix = str((config.stack.get("mesh") or {}).get("tailnet") or "")
    return f"{name}.{suffix}" if suffix else name


def tf_vars(
    config: config_mod.Config,
    env: Mapping[str, str],
    source: secrets_mod.SecretSource,
    *,
    http=None,
) -> dict[str, str]:
    stack = config.stack
    account_id = str((stack.get("cloudflare") or {}).get("account_id") or "")
    backup = stack.get("backup") or {}

    variables: dict[str, str] = {}
    variables["TF_VAR_dokploy_endpoint"] = f"http://{control_host(config)}:3000"
    variables["TF_VAR_dokploy_api_key"] = tofu_mod.require(source, config, "DOKPLOY_API_KEY")
    variables["TF_VAR_registry_pull_token"] = env.get("REGISTRY_PULL_TOKEN") or tofu_mod.optional(
        source, config, "REGISTRY_PULL_TOKEN"
    )
    variables["TF_VAR_admin_user"] = str(stack.get("admin_user") or "")
    variables["TF_VAR_dokploy_ssh_public_key"] = tofu_mod.optional(source, config, "DOKPLOY_SSH_PUBLIC_KEY")
    variables["TF_VAR_dokploy_ssh_private_key"] = tofu_mod.optional(source, config, "DOKPLOY_SSH_PRIVATE_KEY")

    # Object storage (needs.object_storage): R2 bucket per app.
    variables["TF_VAR_cloudflare_api_token"] = env.get("CLOUDFLARE_API_TOKEN") or tofu_mod.optional(
        source, config, "CLOUDFLARE_API_TOKEN"
    )
    variables["TF_VAR_cloudflare_account_id"] = env.get("CLOUDFLARE_ACCOUNT_ID") or account_id
    r2_pair = r2_mod.resolve(source, config, env, http=http, required=False)
    r2_access = r2_pair.access_key_id if r2_pair else ""
    r2_secret = r2_pair.secret_access_key if r2_pair else ""
    variables["TF_VAR_object_storage_access_key_id"] = (
        env.get("OBJECT_STORAGE_ACCESS_KEY_ID") or env.get("BACKUP_ACCESS_KEY_ID") or r2_access
    )
    variables["TF_VAR_object_storage_secret_access_key"] = (
        env.get("OBJECT_STORAGE_SECRET_ACCESS_KEY") or env.get("BACKUP_SECRET_ACCESS_KEY") or r2_secret
    )
    variables["TF_VAR_backup_access_key_id"] = env.get("BACKUP_ACCESS_KEY_ID") or r2_access
    variables["TF_VAR_backup_secret_access_key"] = env.get("BACKUP_SECRET_ACCESS_KEY") or r2_secret

    # Optional second (3-2-1) backup destination: credentials required only when
    # backup.secondary is configured, and read by name (H10).
    if backup.get("secondary"):
        variables["TF_VAR_backup_secondary_access_key_id"] = tofu_mod.require(
            source, config, "BACKUP_SECONDARY_ACCESS_KEY_ID"
        )
        variables["TF_VAR_backup_secondary_secret_access_key"] = tofu_mod.require(
            source, config, "BACKUP_SECONDARY_SECRET_ACCESS_KEY"
        )
    return variables


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    action: str = "plan",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    runner=None,
    http=None,
) -> int:
    env = dict(env or os.environ)
    action = action or "plan"
    if action not in ("plan", "apply", "destroy"):
        raise ValueError(f"platform: unknown action {action!r}")
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)

    platform_dir = Path(config.code_root) / "tofu" / "stacks" / "platform"
    child_env = {**env, **tf_vars(config, env, source, http=http)}

    passphrase = tofu_mod.ready(config, env, source, platform_dir, http=http)
    child_env["TF_VAR_state_passphrase"] = passphrase

    run_tofu = runner or (lambda argv: subprocess.run(argv, env=child_env).returncode)
    return run_tofu(["tofu", "-chdir", str(platform_dir), action])


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    action = (argv or sys.argv[1:] or ["plan"])[0]
    config = config_mod.load_config(env=env)
    try:
        return run(config, env, action)
    except config_mod.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
