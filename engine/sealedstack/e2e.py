"""End-to-end test on a throwaway server (S3). Creates a small VM, bootstraps
it, applies the stacks, checks the v0.1 acceptance criteria, then destroys it.

Replaces ``scripts/e2e.sh``. It runs against a separate config set and state
namespace so production is never touched (N8):

  SEALEDSTACK_CONFIG=<repo>/config-e2e
  SEALEDSTACK_STATE_PREFIX=e2e

WARNING: this spends money and touches real accounts; guarded by ``E2E_CONFIRM``.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from . import buildenv, config as config_mod, edge as edge_mod, platform as platform_mod
from . import postapply, secrets as secrets_mod, server as server_mod, tofu as tofu_mod


def _run_script(code_root: Path, name: str, *args: str) -> int:
    return subprocess.run([sys.executable, str(code_root / "scripts" / name), *args]).returncode


def _destroy(code_root: Path, config, env, source, out) -> bool:
    ok = True
    for stack in ("platform", "edge", "compute"):
        print(f"==> destroying {stack} stack", file=out)
        directory = code_root / "tofu" / "stacks" / stack
        try:
            passphrase = tofu_mod.ready(config, env, source, directory)
            child = {**env, "TF_VAR_state_passphrase": passphrase}
            rc = subprocess.run(["tofu", "-chdir", str(directory), "destroy", "-auto-approve"], env=child).returncode
            if rc != 0:
                ok = False
        except Exception as exc:  # noqa: BLE001
            print(f"sealedstack e2e: destroy of '{stack}' failed: {exc}", file=out)
            ok = False
    return ok


def _port_open(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_code(url: str, timeout: float = 15.0) -> int:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except urllib.error.URLError:
        return 0


def run(env: Optional[dict] = None, out=None) -> int:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout

    if env.get("E2E_CONFIRM") != "yes":
        print(
            "sealedstack e2e: refusing to run.\n"
            "This creates a real server and applies real Cloudflare/Tailscale changes.\n"
            "Set E2E_CONFIRM=yes and provide a throwaway config directory to continue.",
            file=sys.stderr,
        )
        return 2

    code_root = config_mod.find_code_root(env)
    e2e_config = Path(env.get("SEALEDSTACK_E2E_CONFIG") or (code_root / "config-e2e"))
    if not e2e_config.is_dir():
        print(f"sealedstack e2e: create {e2e_config}/ (stack.yaml, servers.yaml, apps.yaml) first", file=sys.stderr)
        return 2
    env["SEALEDSTACK_CONFIG"] = str(e2e_config)
    env.setdefault("SEALEDSTACK_STATE_PREFIX", "e2e")

    config = config_mod.load_config(config_dir=e2e_config, code_root=code_root, env=env)
    source = secrets_mod.build_source(config.stack, config.config_dir, env)
    print(f"==> e2e config: {e2e_config}\n==> e2e state:  {env['SEALEDSTACK_STATE_PREFIX']}/<stack>.tfstate", file=out)

    try:
        _run_script(code_root, "render.py", "--allow-unresolved")
        _run_script(code_root, "validate.py")
        servers = list((config.servers.get("servers") or {}).keys())
        name = servers[0] if servers else ""
        print(f"==> e2e server: {name}", file=out)

        print("==> server-up", file=out)
        server_mod.up(config, env, name, source=source, out=out)

        print("==> apply", file=out)
        _run_script(code_root, "render.py", "--allow-unresolved")
        _run_script(code_root, "mesh-policy.py")
        buildenv.build(config, env, source=secrets_mod.build_source(config.stack, config.config_dir, env, project="prod"))
        from . import userauth

        userauth.run(config, env, source=source, out=out)
        edge_mod.run(config, env, "apply", source=source)
        platform_mod.run(config, env, "apply", source=source)
        postapply.run(config, env, out=out)

        print("==> acceptance checks (spec §15)", file=out)
        domain = str(config.stack.get("domain") or "")
        ips = _compute_ips(code_root, config, env, source)
        ip = ips.get(name, "")
        hello = f"hello.{domain}"
        for port in (22, 80, 443, 3000):
            if _port_open(ip, port):
                print(f"FAIL: {ip}:{port} is open", file=sys.stderr)
                return 1
            print(f"ok: {ip}:{port} closed", file=out)
        code = _http_code(f"https://{hello}")
        if code not in (301, 302):
            print(f"FAIL: {hello} -> {code} (expected 302 to Access)", file=sys.stderr)
            return 1
        print(f"ok: {hello} -> {code}", file=out)
        code = _http_code(f"https://{hello}/.env")
        if code != 403:
            print(f"FAIL: /.env -> {code} (expected 403)", file=sys.stderr)
            return 1
        print("ok: /.env -> 403", file=out)
        print("==> e2e complete", file=out)
        return 0
    finally:
        _destroy(code_root, config, env, source, out)


def _compute_ips(code_root: Path, config, env, source) -> dict:
    directory = code_root / "tofu" / "stacks" / "compute"
    passphrase = tofu_mod.ready(config, env, source, directory)
    child = {**env, "TF_VAR_state_passphrase": passphrase}
    proc = subprocess.run(["tofu", "-chdir", str(directory), "output", "-json", "ips"], capture_output=True, text=True, env=child)
    import json

    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def main(argv: Optional[list[str]] = None) -> int:
    return run(dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
