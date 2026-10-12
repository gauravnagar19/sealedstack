"""``compute`` in Python (spec §4, D-047).

Provision a server (or apply its provider firewall) and print the created
server's public IP. Replaces ``scripts/compute.sh``. Provider credentials are
read from the secret source and exported for the child ``tofu``; the selected
provider's own credential is required (M3), the rest optional.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod

# env var -> secret name (all optional)
OPTIONAL = {
    "TF_VAR_hcloud_token": "HCLOUD_TOKEN",
    "TF_VAR_hostinger_token": "HOSTINGER_TOKEN",
    "TF_VAR_digitalocean_token": "DIGITALOCEAN_TOKEN",
    "TF_VAR_vultr_api_key": "VULTR_API_KEY",
    "TF_VAR_netcup_api_username": "NETCUP_API_USERNAME",
    "TF_VAR_netcup_api_password": "NETCUP_API_PASSWORD",
    "LINODE_TOKEN": "LINODE_TOKEN",
    "SCW_ACCESS_KEY": "SCW_ACCESS_KEY",
    "SCW_SECRET_KEY": "SCW_SECRET_KEY",
    "SCW_DEFAULT_PROJECT_ID": "SCW_DEFAULT_PROJECT_ID",
    "TF_VAR_aws_region": "AWS_REGION",
    "AWS_ACCESS_KEY_ID": "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY": "AWS_SECRET_ACCESS_KEY",
    "TF_VAR_gcp_project": "GCP_PROJECT",
    "TF_VAR_gcp_region": "GCP_REGION",
    "GOOGLE_CREDENTIALS": "GOOGLE_CREDENTIALS",
    "ARM_CLIENT_ID": "ARM_CLIENT_ID",
    "ARM_CLIENT_SECRET": "ARM_CLIENT_SECRET",
    "ARM_SUBSCRIPTION_ID": "ARM_SUBSCRIPTION_ID",
    "ARM_TENANT_ID": "ARM_TENANT_ID",
    "OCI_CLI_USER": "OCI_CLI_USER",
    "OCI_CLI_TENANCY": "OCI_CLI_TENANCY",
    "OCI_CLI_FINGERPRINT": "OCI_CLI_FINGERPRINT",
    "OCI_CLI_KEY_FILE": "OCI_CLI_KEY_FILE",
    "OCI_CLI_REGION": "OCI_CLI_REGION",
    "UPCLOUD_USERNAME": "UPCLOUD_USERNAME",
    "UPCLOUD_PASSWORD": "UPCLOUD_PASSWORD",
    "OVH_ENDPOINT": "OVH_ENDPOINT",
    "OVH_APPLICATION_KEY": "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET": "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY": "OVH_CONSUMER_KEY",
    "CIVO_TOKEN": "CIVO_TOKEN",
    "TF_VAR_contabo_api_username": "CONTABO_CLIENT_ID",
    "TF_VAR_contabo_api_password": "CONTABO_CLIENT_SECRET",
}
DEFAULTS = {"TF_VAR_aws_region": "eu-central-1", "TF_VAR_gcp_region": "europe-west3", "OVH_ENDPOINT": "ovh-eu"}
PROVIDER_REQUIRED = {
    "hetzner": "HCLOUD_TOKEN",
    "hostinger": "HOSTINGER_TOKEN",
    "digitalocean": "DIGITALOCEAN_TOKEN",
    "vultr": "VULTR_API_KEY",
    "linode": "LINODE_TOKEN",
    "scaleway": "SCW_ACCESS_KEY",
    "aws": "AWS_ACCESS_KEY_ID",
    "gcp": "GOOGLE_CREDENTIALS",
    "azure": "ARM_CLIENT_ID",
    "oracle": "OCI_CLI_USER",
    "upcloud": "UPCLOUD_USERNAME",
    "ovh": "OVH_APPLICATION_KEY",
    "civo": "CIVO_TOKEN",
    "contabo": "CONTABO_CLIENT_ID",
    "netcup": "NETCUP_API_USERNAME",
}


def server_provider(config: config_mod.Config, name: str) -> str:
    spec = (config.servers.get("servers") or {}).get(name) or {}
    return str(spec.get("provider") or "")


def tf_vars(config: config_mod.Config, env: Mapping[str, str], source: secrets_mod.SecretSource, name: str) -> dict:
    keys = config.admin_ssh_keys()
    variables = {"TF_VAR_ssh_public_key": keys[0] if keys else ""}
    for var, secret in OPTIONAL.items():
        value = tofu_mod.optional(source, config, secret)
        variables[var] = value or DEFAULTS.get(var, "")
    provider = server_provider(config, name)
    required = PROVIDER_REQUIRED.get(provider)
    if required:
        tofu_mod.require(source, config, required)
    return variables


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    name: str = "",
    action: str = "apply",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    runner=None,
    output_runner=None,
) -> int:
    if not name:
        raise config_mod.ConfigError("usage: compute <name> [plan|apply]")
    action = action or "apply"
    if action not in ("plan", "apply"):
        raise config_mod.ConfigError(f"unknown action {action!r} (expected plan|apply)")
    env = dict(env or os.environ)
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    compute_dir = Path(config.code_root) / "tofu" / "stacks" / "compute"
    plan_file = compute_dir / "sealedstack.tfplan"

    child_env = {**env, **tf_vars(config, env, source, name)}
    passphrase = tofu_mod.ready(config, env, source, compute_dir)
    child_env["TF_VAR_state_passphrase"] = passphrase

    # Tofu local-exec (the firewall adapter) needs the engine importable.
    engine_dir = str(Path(__file__).resolve().parent.parent)
    child_env["PYTHONPATH"] = f"{engine_dir}{os.pathsep}{child_env.get('PYTHONPATH', '')}".rstrip(os.pathsep)

    run_tofu = runner or (lambda argv: subprocess.run(argv, env=child_env).returncode)
    rc = run_tofu(["tofu", "-chdir", str(compute_dir), "plan", "-input=false", f"-out={plan_file}"])
    if rc != 0:
        return rc
    if action == "apply":
        rc = run_tofu(["tofu", "-chdir", str(compute_dir), "show", "-no-color", str(plan_file)])
        if rc != 0:
            return rc
        rc = run_tofu(["tofu", "-chdir", str(compute_dir), "apply", "-input=false", str(plan_file)])
        plan_file.unlink(missing_ok=True)
        if rc != 0:
            return rc

    if action == "apply":
        capture = output_runner or (lambda argv: _capture(argv, child_env))
        rc, stdout = capture(["tofu", "-chdir", str(compute_dir), "output", "-json", "ips"])
        if rc == 0 and stdout:
            try:
                ips = json.loads(stdout)
            except json.JSONDecodeError:
                ips = {}
            print(ips.get(name, ""))
    return 0


def _capture(argv: list, child_env: Mapping[str, str]) -> tuple[int, str]:
    result = subprocess.run(argv, env=dict(child_env), capture_output=True, text=True)
    if result.returncode != 0 and result.stderr.strip():
        print(f"sealedstack: {' '.join(argv)} failed: {result.stderr.strip()}", file=sys.stderr)
    return result.returncode, result.stdout


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: compute <name> [plan|apply]", file=sys.stderr)
        return 2
    name = args[0]
    action = args[1] if len(args) > 1 else "apply"
    config = config_mod.load_config(env=env)
    try:
        return run(config, env, name, action)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
