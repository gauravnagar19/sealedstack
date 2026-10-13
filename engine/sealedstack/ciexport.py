"""Hand a few CI-bootstrap credentials to later workflow steps (masked).

The runner has to join the tailnet *before* the engine can do anything else, but
the Tailscale OAuth client lives in the secret source, not in GitHub (only
``BWS_ACCESS_TOKEN`` does). When workload identity (OIDC) is not configured, the
workflow asks the engine for the client id/secret and passes them to the
``tailscale/github-action`` step as masked step outputs.

Only an allow-list of logical names can be exported, and the value is written to
the step-output file with an ``::add-mask::`` line first; it is never printed.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod

ALLOWED = ("tailscale_oauth_client_id", "tailscale_oauth_client_secret")


def export(
    config: config_mod.Config,
    env: Mapping[str, str],
    logical: str,
    name: str,
    output: Path,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    out=None,
) -> int:
    out = out if out is not None else sys.stdout
    logical = logical.lower()
    if logical not in ALLOWED:
        print(f"sealedstack: '{logical}' cannot be exported (allowed: {', '.join(ALLOWED)})", file=sys.stderr)
        return 2
    if not name.replace("_", "").isalnum():
        print("sealedstack: --name must be alphanumeric", file=sys.stderr)
        return 2
    source = source or secrets_mod.build_source(config.stack, config.config_dir, dict(env))
    result = secrets_mod.lookup(source, config, logical)
    if not result.found:
        print(
            f"sealedstack: {secrets_mod.describe(config, logical)} not found in the '{source.kind}' source",
            file=sys.stderr,
        )
        return 1
    if "\n" in result.value or "\r" in result.value:
        print("sealedstack: refusing to export a multi-line value", file=sys.stderr)
        return 1
    print(f"::add-mask::{result.value}", file=out)
    with open(output, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={result.value}\n")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sealedstack debug ci-export")
    parser.add_argument("logical", help="logical secret name: " + ", ".join(ALLOWED))
    parser.add_argument("--name", required=True, help="step output name")
    parser.add_argument("--output", required=True, help="the GITHUB_OUTPUT file")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    return export(config, env, args.logical, args.name, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
