"""``server`` in Python — bring a server into the sealed stack (D-047).

``up`` replaces ``scripts/server-up.sh``: provision (when the provider creates the
server) or read the bootstrap IP/password from the secret source (D-049), mint a
mesh auth key, bootstrap over SSH, run ``site.yml`` over the mesh, then apply the
provider firewall (H5). ``sync`` re-runs ``site.yml`` only.

The public IP is never an argument or config (D-049); it is read from the secret
source and never printed. Secrets reach Ansible through a 0600 ``-e @file``, never
argv (H10).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod

# run: inherit stdio, return rc. capture: capture stdout, return (rc, stdout).
RunFn = Callable[[list, Mapping[str, str]], int]
CaptureFn = Callable[[list, Mapping[str, str]], "tuple[int, str]"]


@dataclass
class ServerIO:
    run: RunFn
    capture: CaptureFn
    prompt: Callable[[str], str]


def _default_io() -> ServerIO:
    def run(argv, env):
        return subprocess.run(argv, env=dict(env)).returncode

    def capture(argv, env):
        proc = subprocess.run(argv, env=dict(env), capture_output=True, text=True)
        return proc.returncode, proc.stdout

    def prompt(message):
        # CI has no terminal: print the instruction and carry on instead of
        # crashing on EOF after the server is already up.
        if sys.stdin is not None and sys.stdin.isatty():
            return input(message)
        print(f"{message}(non-interactive run: continuing)", flush=True)
        return ""

    return ServerIO(run=run, capture=capture, prompt=prompt)


def write_vars(path: Path, values: Mapping[str, str]) -> None:
    """Write a 0600 JSON vars file (valid ``-e @file``); empty values dropped."""
    data = {k: v for k, v in values.items() if v}
    fd = os.open(str(path), os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data))


def registry_value(code_root: Path, slot: str, impl_id: str, field: str, default: str = "") -> str:
    import yaml

    path = Path(code_root) / "registry" / f"{slot}.yaml"
    if not path.exists():
        return default
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for entry in doc.get("implementations") or []:
        if entry.get("id") == impl_id:
            value = entry.get(field)
            return default if value is None else str(value)
    return default


def creates_server(code_root: Path, provider: str) -> bool:
    return registry_value(code_root, "compute", provider, "creates_server", "false").lower() == "true"


def server_field(config: config_mod.Config, name: str, field: str) -> str:
    spec = (config.servers.get("servers") or {}).get(name) or {}
    return str(spec.get(field) or "")


def _require(source: secrets_mod.SecretSource, key: str) -> str:
    result = source.get(key)
    if not result.found:
        raise config_mod.ConfigError(f"missing required secret '{key}' in the '{source.kind}' source")
    return result.value


def up(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    name: str = "",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    io: Optional[ServerIO] = None,
    out=None,
) -> str:
    """Bootstrap a server; returns the redacted success message."""
    if not name:
        raise config_mod.ConfigError("usage: server up <name>")
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    io = io or _default_io()
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    code_root = Path(config.code_root)

    provider = server_field(config, name, "provider")
    admin_user = str(config.stack.get("admin_user") or "")
    ip_key, pw_key = config.bootstrap_secrets(name)

    root_password = ""
    ip = ""
    already_bootstrapped = False
    if creates_server(code_root, provider):
        print(f"==> provisioning {name} via {provider}", file=out)
        rc, stdout = io.capture([sys.executable, "-m", "sealedstack.compute", name, "apply"], env)
        if rc != 0:
            raise config_mod.ConfigError(f"compute apply failed for {name} (exit {rc})")
        ip = stdout.strip().splitlines()[-1] if stdout.strip() else ""
    elif not source.get(ip_key).found and not source.get(pw_key).found:
        # D-049: both bootstrap secrets are deleted after the first bootstrap, so
        # their absence means "already bootstrapped". Skip the public-SSH bootstrap
        # and only re-assert the baseline over the mesh; this keeps `up` re-runnable.
        already_bootstrapped = True
        print(
            f"==> {name}: no bootstrap secrets ({ip_key}, {pw_key}); assuming it is already "
            "bootstrapped and re-asserting the baseline over the mesh",
            file=out,
        )
    else:
        ip = _require(source, ip_key)
        root_password = _require(source, pw_key)

    if not ip and not already_bootstrapped:
        raise config_mod.ConfigError(f"could not determine the public IP for '{name}'")
    if not already_bootstrapped:
        print(f"==> {name}: using the bootstrap IP from the secret source (redacted)", file=out)

    import tempfile

    auth_key = ""
    if not already_bootstrapped:
        auth_key = _capture_required(
            io,
            [sys.executable, "-m", "sealedstack.tailscalekey"],
            {
                **env,
                "TAILSCALE_OAUTH_CLIENT_ID": tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_ID"),
                "TAILSCALE_OAUTH_CLIENT_SECRET": tofu_mod.require(source, config, "TAILSCALE_OAUTH_CLIENT_SECRET"),
            },
        )
    dokploy_ssh_public_key = _capture_required(io, [sys.executable, "-m", "sealedstack.dokploykey"], env)
    admin_ssh_keys_json = json.dumps(config.admin_ssh_keys())

    fd, vars_path = tempfile.mkstemp()
    os.close(fd)
    vars_file = Path(vars_path)
    try:
        if not already_bootstrapped:
            write_vars(
                vars_file,
                {
                    "target_name": name,
                    "tailscale_auth_key": auth_key,
                    "admin_user": admin_user,
                    "admin_ssh_keys_json": admin_ssh_keys_json,
                    "ansible_password": root_password or env.get("ROOT_PASSWORD", ""),
                },
            )
            bootstrap_args = ["-e", f"@{vars_file}"]
            if not root_password and not env.get("ROOT_PASSWORD"):
                bootstrap_args.append("--ask-pass")

            print(f"==> bootstrap {name}", file=out)
            rc = io.run(
                ["ansible-playbook", "-i", f"{ip},", "-u", "root", str(code_root / "ansible" / "playbooks" / "bootstrap.yml"), *bootstrap_args],
                env,
            )
            if rc != 0:
                raise config_mod.ConfigError(f"bootstrap failed for {name} (exit {rc})")

        write_vars(
            vars_file,
            {
                "dokploy_ssh_public_key": dokploy_ssh_public_key,
                "admin_ssh_keys_json": admin_ssh_keys_json,
            },
        )
        print(f"==> site {name} over Tailscale", file=out)
        rc = io.run(
            [
                "ansible-playbook",
                "-i",
                str(code_root / "ansible" / "inventory.py"),
                "-e",
                f"@{vars_file}",
                "--limit",
                name,
                str(code_root / "ansible" / "playbooks" / "site.yml"),
            ],
            env,
        )
        if rc != 0:
            hint = (
                f"; if '{name}' was never bootstrapped, add {ip_key} and {pw_key} to the secret source"
                if already_bootstrapped
                else ""
            )
            raise config_mod.ConfigError(f"site.yml failed for {name} (exit {rc}){hint}")
    finally:
        vars_file.unlink(missing_ok=True)

    if already_bootstrapped:
        print(_checklist(name), file=out)
        return f"sealedstack: server '{name}' is up"
    firewall_mode = registry_value(code_root, "compute", provider, "firewall", "manual")
    if not creates_server(code_root, provider) and firewall_mode in ("native", "api"):
        print(f"==> applying provider firewall for {name} ({firewall_mode})", file=out)
        rc = io.run([sys.executable, "-m", "sealedstack.compute", name, "apply"], env)
        if rc != 0:
            raise config_mod.ConfigError(f"firewall apply failed for {name} (exit {rc})")
    if firewall_mode == "manual":
        print(
            "\nConfirm the firewall rule table in docs/netcup-firewall.md is applied, then "
            "remove the temporary inbound TCP 22 rule on the provider.",
            file=out,
        )
        io.prompt("Press Enter when done… ")

    print(_checklist(name), file=out)
    return f"sealedstack: server '{name}' is up"


def sync(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    name: str = "",
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    io: Optional[ServerIO] = None,
) -> int:
    """Re-run ``site.yml`` over the mesh (drift correction, upgrades)."""
    env = dict(env or os.environ)
    io = io or _default_io()
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    import tempfile

    # D-055: the admin user has passwordless sudo, so no secret is needed here;
    # only the (public) operator keys, so the ssh role re-asserts them.
    fd, vars_path = tempfile.mkstemp()
    os.close(fd)
    vars_file = Path(vars_path)
    try:
        write_vars(vars_file, {"admin_ssh_keys_json": json.dumps(config.admin_ssh_keys())})
        argv = ["ansible-playbook", "-i", str(Path(config.code_root) / "ansible" / "inventory.py")]
        if name:
            argv += ["--limit", name]
        argv += ["-e", f"@{vars_file}", str(Path(config.code_root) / "ansible" / "playbooks" / "site.yml")]
        return io.run(argv, env)
    finally:
        vars_file.unlink(missing_ok=True)


def _capture_required(io: ServerIO, argv: list, env: Mapping[str, str]) -> str:
    rc, stdout = io.capture(argv, env)
    if rc != 0:
        raise config_mod.ConfigError(f"{argv[0]} failed (exit {rc})")
    return stdout.strip()


def _checklist(name: str) -> str:
    return (
        f"\nsealedstack: first-run checklist for '{name}'\n"
        f"  1. Open the Dokploy panel over the mesh (http://{name}:3000) and create the first admin account.\n"
        "  2. In Dokploy: Settings > Profile > API/CLI Keys, generate a key and store it as DOKPLOY_API_KEY in the 'infra' secrets project.\n"
        "  3. Store STATE_PASSPHRASE in the 'infra' project (the R2 keys are derived from CLOUDFLARE_API_TOKEN), then run 'sealedstack up' (it creates the state bucket).\n"
        "  4. Confirm the provider firewall rule table and close the temporary inbound TCP 22 window (docs/netcup-firewall.md).\n"
        "  5. Run 'sealedstack check' to gate every step above.\n"
    )


def main(argv: Optional[list[str]] = None) -> int:
    env = dict(os.environ)
    args = list(argv if argv is not None else sys.argv[1:])
    action = args[0] if args else "up"
    name = args[1] if len(args) > 1 else ""
    config = config_mod.load_config(env=env)
    try:
        if action == "sync":
            return sync(config, env, name)
        up(config, env, name)
        return 0
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
