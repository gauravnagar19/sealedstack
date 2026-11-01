"""OpenTofu backend init (D-047).

Replaces the ``tofu_ready`` helper in ``scripts/lib.sh``. The R2 backend
credentials go into a throwaway 0600 ``-backend-config`` file that is removed
right after ``init`` and are never exported: exporting them would clobber
``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``, which the AWS provider uses
for ``provider: aws`` in the compute stack (M-review item 5).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import config as config_mod
from . import r2 as r2_mod
from . import secrets as secrets_mod

# A tofu runner takes (argv, env) and returns a return code.
TofuRunner = Callable[[Sequence[str], Mapping[str, str]], int]


def unattended(env: Mapping[str, str]) -> bool:
    """True when no person can answer a prompt: CI, or no terminal on stdin."""
    if str(env.get("CI", "")).lower() in ("1", "true") or env.get("GITHUB_ACTIONS"):
        return True
    try:
        return not sys.stdin.isatty()
    except (AttributeError, ValueError):
        return True


def action_args(action: str, env: Mapping[str, str]) -> list:
    """Flags for ``tofu <action>``.

    ``apply`` waits for a typed "yes" unless told not to, which in CI ends in "error asking for approval: EOF"
    (the first live ``up`` died there). The approval that matters is sealedstack's own gate (D-052), which has
    already passed by the time a phase runs, so unattended applies are automatic and never prompt. A person at a
    terminal still gets OpenTofu's prompt. ``destroy`` is never made automatic.
    """
    if action == "apply" and unattended(env):
        return ["-input=false", "-auto-approve"]
    if action in ("plan", "apply") and unattended(env):
        return ["-input=false"]
    return []


def default_runner(argv: Sequence[str], env: Mapping[str, str]) -> int:
    return subprocess.run(list(argv), env=dict(env)).returncode


def require(source: secrets_mod.SecretSource, config: config_mod.Config, name: str) -> str:
    """Read a required infra secret (honouring the D-050 key mapping)."""
    result = secrets_mod.lookup(source, config, name)
    if not result.found:
        raise config_mod.ConfigError(
            f"sealedstack: missing required secret {secrets_mod.describe(config, name)} "
            f"in the '{source.kind}' source (project '{config.source_project()}')"
        )
    return result.value


def optional(source: secrets_mod.SecretSource, config: config_mod.Config, name: str) -> str:
    result = secrets_mod.lookup(source, config, name)
    return result.value if result.found else ""


def backend_config(
    config: config_mod.Config,
    env: Mapping[str, str],
    source: secrets_mod.SecretSource,
    stack_dir: Path,
    *,
    http=None,
) -> tuple[Path, str]:
    """Write the 0600 backend config; return ``(path, state_passphrase)``.

    The R2 credentials are derived from the Cloudflare token (D-054) unless an
    explicit override pair exists in the secret source.
    """
    import tempfile

    account_id = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
    state_bucket = str((config.stack.get("state") or {}).get("bucket") or "sealedstack-state")

    creds = r2_mod.resolve(source, config, env, http=http)
    r2_access, r2_secret = creds.access_key_id, creds.secret_access_key
    passphrase = require(source, config, "state_passphrase")
    if len(passphrase) < 16:
        raise config_mod.ConfigError(
            f"sealedstack: state_passphrase must be at least 16 characters (pbkdf2); got {len(passphrase)}"
        )

    # State key: sealedstack/<infra_name>/<stack>.tfstate (spec §6.3a). The infra
    # name comes from stack.yaml:name; e2e overrides it with
    # SEALEDSTACK_STATE_PREFIX so it can share the bucket without touching
    # production state (N8).
    infra_name = env.get("SEALEDSTACK_STATE_PREFIX") or config.name or "sealedstack"
    stack = Path(stack_dir).name
    key = f"sealedstack/{infra_name}/{stack}.tfstate"

    content = (
        f'bucket = "{state_bucket}"\n'
        f'key = "{key}"\n'
        'region = "auto"\n'
        f'endpoints = {{ s3 = "https://{account_id}.r2.cloudflarestorage.com" }}\n'
        f'access_key = "{r2_access}"\n'
        f'secret_key = "{r2_secret}"\n'
        "skip_credentials_validation = true\n"
        "skip_region_validation = true\n"
        "skip_requesting_account_id = true\n"
        "use_path_style = true\n"
        # R2 rejects the checksum trailers newer S3 clients add to uploads (HTTP 501), so state writes need this off.
        "skip_s3_checksum = true\n"
        "use_lockfile = true\n"
    )

    fd, path = tempfile.mkstemp()
    os.chmod(path, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return Path(path), passphrase


def ready(
    config: config_mod.Config,
    env: Mapping[str, str],
    source: secrets_mod.SecretSource,
    stack_dir: Path,
    *,
    runner: Optional[TofuRunner] = None,
    http=None,
) -> str:
    """``tofu init`` a stack against the encrypted R2 backend.

    Returns the state passphrase, which every later ``tofu`` command on the stack
    needs (the encryption block reads ``TF_VAR_state_passphrase``).
    """
    runner = runner or default_runner
    # Drop any legacy plaintext backend file (H10).
    legacy = Path(stack_dir) / "backend.hcl"
    if legacy.exists():
        legacy.unlink()

    tmp, passphrase = backend_config(config, env, source, stack_dir, http=http)
    child_env = dict(env)
    child_env["TF_VAR_state_passphrase"] = passphrase
    try:
        rc = runner(
            ["tofu", "-chdir", str(stack_dir), "init", "-input=false", "-reconfigure", f"-backend-config={tmp}"],
            child_env,
        )
    finally:
        tmp.unlink(missing_ok=True)
    if rc != 0:
        raise config_mod.ConfigError(f"tofu init failed for {stack_dir} (exit {rc})")
    return passphrase
