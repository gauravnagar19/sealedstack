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

ALLOWED = (
    "tailscale_oauth_client_id", "tailscale_oauth_client_secret",
    "tailscale_ci_oauth_client_id", "tailscale_ci_oauth_client_secret",
)
# D-072: a Tailscale OAuth client can only mint keys for exactly its own tag set, so the client that creates `tag:server`
# keys (policy, bootstrap) cannot also join CI as `tag:ci`. The CI join reads its own client; users with a single
# client keep working (it is then the same pair, which only suits a client whose tags are exactly [tag:ci]).
CI_FALLBACK = {
    "tailscale_ci_oauth_client_id": "tailscale_oauth_client_id",
    "tailscale_ci_oauth_client_secret": "tailscale_oauth_client_secret",
}


def export_tailscale_oidc(config: config_mod.Config, output: Path) -> int:
    """Write the (non-secret) OIDC trust settings from ``stack.yaml`` as step outputs (FR-5).

    The runner joins the tailnet before the engine does anything else, and it must not
    parse ``stack.yaml`` itself (PyYAML is not guaranteed on a runner image), so the
    workflow asks the engine image: ``client_id=`` and ``audience=`` (empty when unset)."""
    mesh = (config.stack.get("providers") or {}).get("mesh_tailscale") or {}
    lines = []
    for name, key in (("client_id", "oauth_client_id"), ("audience", "audience")):
        value = str(mesh.get(key) or "")
        if "\n" in value or "\r" in value:
            print(f"sealedstack: providers.mesh_tailscale.{key} must be a single line", file=sys.stderr)
            return 1
        lines.append(f"{name}={value}\n")
    with open(output, "a", encoding="utf-8") as handle:
        handle.writelines(lines)
    return 0


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
    if not result.found and logical in CI_FALLBACK:
        result = secrets_mod.lookup(source, config, CI_FALLBACK[logical])
    if not result.found:
        wanted = secrets_mod.describe(config, logical)
        if logical in CI_FALLBACK:
            wanted += f" (or {secrets_mod.describe(config, CI_FALLBACK[logical])})"
        print(f"sealedstack: {wanted} not found in the '{source.kind}' source", file=sys.stderr)
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
    parser.add_argument("logical", nargs="?", help="logical secret name: " + ", ".join(ALLOWED))
    parser.add_argument("--name", help="step output name")
    parser.add_argument("--output", required=True, help="the GITHUB_OUTPUT file")
    parser.add_argument("--tailscale-oidc", action="store_true",
                        help="write the non-secret providers.mesh_tailscale oauth_client_id / audience as client_id= and audience=")
    args = parser.parse_args(argv)
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    if args.tailscale_oidc:
        return export_tailscale_oidc(config, Path(args.output))
    if not args.logical or not args.name:
        parser.error("a logical secret name and --name are required (or use --tailscale-oidc)")
    return export(config, env, args.logical, args.name, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
