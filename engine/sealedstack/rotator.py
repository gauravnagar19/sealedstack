"""Rotate secrets (spec §13, D-047). Recreates managed secrets through OpenTofu
where possible; prints the manual, provider-side steps otherwise. Replaces
``scripts/rotate-secrets.sh``.

  rotate tunnel [app]   # new Cloudflare tunnel token
  rotate db [app]       # new managed database password
  rotate tailscale-key  # mint a fresh tag:server auth key
  rotate all
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import edge as edge_mod
from . import platform as platform_mod
from . import secrets as secrets_mod
from . import tailscalekey as ts_key_mod

USAGE = """usage: rotate <tunnel|db|tailscale-key|all> [app]

Still manual (provider-side):
  - CLOUDFLARE_API_TOKEN, TAILSCALE_OAUTH_CLIENT_SECRET : revoke + recreate in the dashboards,
    then update the `infra` secrets. `sealedstack doctor` re-checks them.
  - DOKPLOY_API_KEY, admin_sudo_password    : regenerate in Dokploy / on the host,
    then update Secrets Manager (DOKPLOY_API_KEY) and `admin_sudo_password`.
  - R2 keys : derived from CLOUDFLARE_API_TOKEN, so rotating that token rotates them (the
    state bucket and backup credentials follow on the next apply).
"""


def _tofu(args: list, cwd: Path, env: Mapping[str, str]) -> tuple[int, str]:
    proc = subprocess.run(["tofu", "-chdir", str(cwd), *args], capture_output=True, text=True, env=dict(env))
    return proc.returncode, proc.stdout


def _taint(config: config_mod.Config, stack: str, pattern: str, app: str, addr: str, env: Mapping[str, str], out) -> None:
    directory = Path(config.code_root) / "tofu" / "stacks" / stack
    if app:
        _tofu(["taint", addr], directory, env)
        return
    _, listing = _tofu(["state", "list"], directory, env)
    for line in listing.splitlines():
        if pattern in line:
            _tofu(["taint", line.strip()], directory, env)


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    action: str = "",
    app: str = "",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    out=None,
) -> int:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)

    if action == "tunnel":
        print("tainting Cloudflare tunnels", file=out)
        _taint(config, "edge", "cloudflare_zero_trust_tunnel_cloudflared.this", app,
               f'module.ingress["{app}"].cloudflare_zero_trust_tunnel_cloudflared.this', env, out)
        return edge_mod.run(config, env, "apply", source=source)
    if action == "db":
        print("tainting managed database passwords", file=out)
        _taint(config, "platform", "random_password.db", app,
               f'module.app["{app}"].random_password.db[0]', env, out)
        return platform_mod.run(config, env, "apply", source=source)
    if action == "tailscale-key":
        from . import tofu as tofu_mod

        key_env = {
            **env,
            "TAILSCALE_OAUTH_CLIENT_ID": tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_ID"),
            "TAILSCALE_OAUTH_CLIENT_SECRET": tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_SECRET"),
        }
        print(ts_key_mod.run(key_env), file=out)
        return 0
    if action == "all":
        rc = run(config, env, "tunnel", "", source=source, out=out)
        return rc or run(config, env, "db", "", source=source, out=out)
    print(USAGE, file=sys.stderr)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    action = args[0] if args else ""
    app = args[1] if len(args) > 1 else ""
    config = config_mod.load_config(env=dict(os.environ))
    try:
        return run(config, dict(os.environ), action, app)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
