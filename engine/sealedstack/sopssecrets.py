"""Write secrets to a SOPS-encrypted file (registry secrets: sops). The
zero-account option for the open-source path; the encrypted file is committed.
Replaces ``scripts/sops-secrets.sh``.

Inputs: ``SECRETS_JSON``, ``SOPS_AGE_RECIPIENT``, ``SOPS_FILE``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Mapping, Optional

import yaml


def run(env: Optional[Mapping[str, str]] = None, out=None) -> int:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    raw = env.get("SECRETS_JSON", "")
    recipient = env.get("SOPS_AGE_RECIPIENT", "")
    if not raw or not recipient:
        print("sops-secrets: SECRETS_JSON and SOPS_AGE_RECIPIENT must be set", file=sys.stderr)
        return 2
    try:
        secrets = json.loads(raw)
    except json.JSONDecodeError:
        print("sops-secrets: could not parse SECRETS_JSON", file=sys.stderr)
        return 1

    config_dir = Path(env.get("SEALEDSTACK_CONFIG") or "config")
    file = Path(env.get("SOPS_FILE") or config_dir / "secrets" / "infra.enc.yaml")
    file.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
        tmp.write(yaml.safe_dump({str(k): str(v) for k, v in secrets.items()}, sort_keys=False))
        plain = tmp.name
    try:
        rc = subprocess.run(
            ["sops", "--encrypt", "--age", recipient, "--input-type", "yaml", "--output", str(file), plain]
        ).returncode
    finally:
        os.unlink(plain)
    if rc != 0:
        return rc
    print(f"wrote {file}", file=out)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return run(dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
