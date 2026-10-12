"""Ensure the Dokploy->worker SSH key exists; print the public key (D-047).

Replaces ``scripts/ensure-dokploy-key.sh``. On first use it generates an ed25519
keypair and writes both halves to the secret source (Bitwarden). Writing a secret
is the documented bootstrap exception to the read-only rule (D-038): this is the
only value sealedstack creates in the secret source.

H10 limitation (bws 2.1.0): ``bws secret create`` takes the value only as a
positional argument, so the generated private key is briefly visible in the local
process list. It is created once and only ever leaves this machine to Bitwarden.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod


def _bws(argv: list, env: Mapping[str, str]) -> tuple[int, str, str]:
    proc = subprocess.run(["bws", *argv], capture_output=True, text=True, env=dict(env))
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    out=None,
    generate=None,
) -> str:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)

    private = tofu_mod.optional(source, config, "DOKPLOY_SSH_PRIVATE_KEY")
    if private:
        public = tofu_mod.require(source, config, "DOKPLOY_SSH_PUBLIC_KEY")
        print(public, file=out)
        return public

    generate = generate or _generate_keypair
    private_pem, public_pem = generate()

    token = env.get("BWS_ACCESS_TOKEN", "")
    if not token:
        raise config_mod.ConfigError("BWS_ACCESS_TOKEN must be set to store the Dokploy worker key")
    rc, listing, err = _bws(["project", "list", "--output", "json"], env)
    if rc != 0:
        raise config_mod.ConfigError(f"bws project list failed: {err or listing}")
    projects = json.loads(listing or "[]")
    project_id = next((p.get("id", "") for p in projects if p.get("name") == "infra"), "")
    if not project_id:
        raise config_mod.ConfigError("Bitwarden project 'infra' not found")

    for key, value in (("DOKPLOY_SSH_PRIVATE_KEY", private_pem), ("DOKPLOY_SSH_PUBLIC_KEY", public_pem)):
        rc, _, err = _bws(["secret", "create", key, value, project_id], env)
        if rc != 0:
            raise config_mod.ConfigError(f"bws secret create {key} failed: {err}")

    print(public_pem.strip(), file=out)
    return public_pem.strip()


def _generate_keypair() -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as tmp:
        key = Path(tmp) / "id"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "sealedstack-dokploy", "-f", str(key)],
            check=True,
            capture_output=True,
        )
        return key.read_text(encoding="utf-8"), key.with_suffix(".pub").read_text(encoding="utf-8")


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        run(config, env)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
