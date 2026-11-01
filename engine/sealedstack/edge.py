"""``edge`` in Python (spec section 8, D-047).

Plan or apply the edge stack and, on apply, write ``.build/edge-outputs.json`` so
the platform stack can read the connector tokens. Replaces ``scripts/edge.sh``.

Secrets are assembled into ``TF_VAR_*`` environment variables for the child
``tofu`` process; a required secret that is missing aborts before any change (N3).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import r2 as r2_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod

# (TF_VAR name, config/env fallback): the secrets-slot / mesh providers that read
# their settings from the environment rather than the secrets store.
PLAIN_TFVARS = {
    "TF_VAR_zerotier_api_token": "ZEROTIER_API_TOKEN",
    "TF_VAR_gcp_project": "GCP_PROJECT",
    "TF_VAR_azure_key_vault_id": "AZURE_KEY_VAULT_ID",
    "TF_VAR_openbao_addr": "OPENBAO_ADDR",
    "TF_VAR_openbao_token": "OPENBAO_TOKEN",
    "TF_VAR_vault_addr": "VAULT_ADDR",
    "TF_VAR_vault_token": "VAULT_TOKEN",
}


def tf_vars(
    config: config_mod.Config,
    env: Mapping[str, str],
    source: secrets_mod.SecretSource,
    *,
    http=None,
) -> dict[str, str]:
    """Build the ``TF_VAR_*`` map the edge stack reads."""
    stack = config.stack
    account_id = str((stack.get("cloudflare") or {}).get("account_id") or "")
    backup = stack.get("backup") or {}

    variables: dict[str, str] = {}

    variables["TF_VAR_cloudflare_api_token"] = tofu_mod.require(source, config, "CLOUDFLARE_API_TOKEN")
    variables["TF_VAR_tailscale_oauth_client_id"] = tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_ID")
    variables["TF_VAR_tailscale_oauth_client_secret"] = tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_SECRET")

    variables["TF_VAR_backup_endpoint"] = (
        env.get("BACKUP_ENDPOINT")
        or str(backup.get("endpoint") or "")
        or f"https://{account_id}.r2.cloudflarestorage.com"
    )
    variables["TF_VAR_backup_region"] = env.get("BACKUP_REGION") or str(backup.get("region") or "auto")
    r2_pair = None
    if not (env.get("BACKUP_ACCESS_KEY_ID") and env.get("BACKUP_SECRET_ACCESS_KEY")):
        r2_pair = r2_mod.resolve(source, config, env, http=http, required=False)
    variables["TF_VAR_backup_access_key_id"] = env.get("BACKUP_ACCESS_KEY_ID") or (
        r2_pair.access_key_id if r2_pair else ""
    )
    variables["TF_VAR_backup_secret_access_key"] = env.get("BACKUP_SECRET_ACCESS_KEY") or (
        r2_pair.secret_access_key if r2_pair else ""
    )

    variables["TF_VAR_ssm_prefix"] = env.get("SSM_PREFIX", "/sealedstack/infra")
    variables["TF_VAR_ssm_region"] = env.get("SSM_REGION", "eu-central-1")
    variables["TF_VAR_ssm_access_key_id"] = env.get("SSM_ACCESS_KEY_ID") or env.get("AWS_ACCESS_KEY_ID", "")
    variables["TF_VAR_ssm_secret_access_key"] = env.get("SSM_SECRET_ACCESS_KEY") or env.get("AWS_SECRET_ACCESS_KEY", "")

    for var, key in PLAIN_TFVARS.items():
        variables[var] = env.get(key, "")

    variables["TF_VAR_ngrok_api_key"] = env.get("NGROK_API_KEY") or tofu_mod.optional(source, config, "NGROK_API_KEY")
    return variables


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    action: str = "plan",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    runner=None,
    output_runner=None,
    http=None,
) -> int:
    """Init the backend, then run ``tofu plan|apply`` and write the outputs."""
    env = dict(env or os.environ)
    action = action or "plan"
    if action not in ("plan", "apply", "destroy"):
        raise ValueError(f"edge: unknown action {action!r}")
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)

    edge_dir = Path(config.code_root) / "tofu" / "stacks" / "edge"
    child_env = {**env, **tf_vars(config, env, source, http=http)}

    passphrase = tofu_mod.ready(config, env, source, edge_dir, http=http)
    child_env["TF_VAR_state_passphrase"] = passphrase

    run_tofu = runner or (lambda argv: subprocess.run(argv, env=child_env).returncode)
    rc = run_tofu(["tofu", "-chdir", str(edge_dir), action, *tofu_mod.action_args(action, env)])
    if rc != 0:
        return rc

    if action == "apply":
        out = Path(config.code_root) / ".build" / "edge-outputs.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        capture = output_runner or (
            lambda argv: _capture(argv, child_env)
        )
        rc, stdout = capture(["tofu", "-chdir", str(edge_dir), "output", "-json"])
        if rc != 0:
            return rc
        out.write_text(json.dumps(plain_outputs(stdout)), encoding="utf-8")
        print(f"edge outputs -> {out.relative_to(Path(config.code_root))}")
    return 0


def plain_outputs(stdout: str) -> dict:
    """``tofu output -json`` wraps every output as ``{"value": ..., "type": ..., "sensitive": ...}``.

    The platform stack reads ``edge.backup.bucket`` and ``edge.connector_tokens`` directly, so the file it reads must hold the
    plain values (the first live ``up`` handed it the wrappers: "This object does not have an attribute named bucket")."""
    data = json.loads(stdout) if (stdout or "").strip() else {}
    return {
        name: item["value"] if isinstance(item, dict) and "value" in item and "type" in item else item
        for name, item in data.items()
    }


def _capture(argv: list[str], child_env: Mapping[str, str]) -> tuple[int, str]:
    result = subprocess.run(argv, env=dict(child_env), capture_output=True, text=True)
    if result.returncode != 0 and result.stderr.strip():
        print(f"edge: {' '.join(argv)} failed: {result.stderr.strip()}", file=sys.stderr)
    return result.returncode, result.stdout


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
