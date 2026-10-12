"""Write secrets through a provider CLI (registry secrets: infisical, doppler,
pass, onepassword). One generic adapter, dispatched by ``SECRETS_PROVIDER``.
Replaces ``scripts/secrets-cli.sh``. Values reach the CLI via a 0600 file or
stdin, never argv (H10).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Mapping, Optional


def _run(argv: list, env: Mapping[str, str]) -> int:
    return subprocess.run(argv, env=dict(env)).returncode


def write_secret(provider: str, key: str, value: str, env: Mapping[str, str]) -> int:
    project = env.get("SECRETS_PROJECT", "")
    if provider == "infisical":
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
            tmp.write(f"{key}={value}\n")
            path = tmp.name
        try:
            return _run(
                ["infisical", "secrets", "set", "--file", path, "--projectId", project,
                 "--env", env.get("SECRETS_ENV", "prod"), "--silent"],
                env,
            )
        finally:
            os.unlink(path)
    if provider == "doppler":
        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as tmp:
            tmp.write(f"{key}={value}\n")
            path = tmp.name
        try:
            return _run(
                ["doppler", "secrets", "upload", path, "--project", project,
                 "--config", env.get("SECRETS_ENV", "prd"), "--silent"],
                env,
            )
        finally:
            os.unlink(path)
    if provider == "pass":
        path = f"{project}/{key}" if project else key
        proc = subprocess.run(["pass", "insert", "-m", "-f", path], input=(value + "\n").encode("utf-8"), env=dict(env))
        return proc.returncode
    if provider == "onepassword":
        template = json.dumps(
            {"title": key, "category": "PASSWORD",
             "fields": [{"id": "password", "type": "CONCEALED", "value": value}]}
        )
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as tmp:
            tmp.write(template)
            path = tmp.name
        try:
            return _run(["op", "item", "create", "--template", path, "--vault", env.get("SECRETS_VAULT", "")], env)
        finally:
            os.unlink(path)
    print(f"secrets-cli: unknown SECRETS_PROVIDER: {provider}", file=sys.stderr)
    return 2


def run(env: Optional[Mapping[str, str]] = None, out=None) -> int:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    provider = env.get("SECRETS_PROVIDER", "")
    raw = env.get("SECRETS_JSON", "")
    if not provider or not raw:
        print("secrets-cli: SECRETS_PROVIDER and SECRETS_JSON must be set", file=sys.stderr)
        return 2
    try:
        secrets = json.loads(raw)
    except json.JSONDecodeError:
        print("secrets-cli: could not parse SECRETS_JSON", file=sys.stderr)
        return 1
    for key, value in secrets.items():
        rc = write_secret(provider, str(key), str(value), env)
        if rc != 0:
            return rc
    print(f"wrote secrets via {provider}", file=out)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return run(dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
