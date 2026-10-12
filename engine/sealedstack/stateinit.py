"""``state-init`` in Python (spec §10, D-047).

Create the encrypted state bucket once. The state stack uses **local** state, so
this does not touch the R2 backend config. Replaces ``scripts/state-init.sh``.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod


def _require(source: secrets_mod.SecretSource, config: config_mod.Config, name: str) -> str:
    return tofu_mod.require(source, config, name)


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    extra_args: Optional[list[str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    runner=None,
) -> int:
    env = dict(env or os.environ)
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    state_dir = Path(config.code_root) / "tofu" / "stacks" / "state"
    child_env = dict(env)
    child_env["TF_VAR_account_id"] = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
    child_env["TF_VAR_bucket"] = str((config.stack.get("state") or {}).get("bucket") or "")
    child_env["TF_VAR_r2_access_key_id"] = _require(source, config, "R2_ACCESS_KEY_ID")
    child_env["TF_VAR_r2_secret_access_key"] = _require(source, config, "R2_SECRET_ACCESS_KEY")
    passphrase = _require(source, config, "state_passphrase")
    if len(passphrase) < 16:
        raise config_mod.ConfigError(
            f"state_passphrase must be at least 16 characters (pbkdf2); got {len(passphrase)}"
        )
    child_env["TF_VAR_state_passphrase"] = passphrase

    run_tofu = runner or (lambda argv: subprocess.run(argv, env=child_env).returncode)
    rc = run_tofu(["tofu", "-chdir", str(state_dir), "init"])
    if rc != 0:
        return rc
    return run_tofu(["tofu", "-chdir", str(state_dir), "apply", *(extra_args or [])])


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    args = list(argv if argv is not None else sys.argv[1:])
    config = config_mod.load_config(env=env)
    try:
        return run(config, env, args)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
