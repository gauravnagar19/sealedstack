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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import config as config_mod
from . import generated as generated_mod
from . import hostkey as hostkey_mod
from . import secrets as secrets_mod
from . import tailnet as tailnet_mod
from . import tofu as tofu_mod

# run: inherit stdio, return rc. capture: capture stdout, return (rc, stdout).
RunFn = Callable[[list, Mapping[str, str]], int]
CaptureFn = Callable[[list, Mapping[str, str]], "tuple[int, str]"]


@dataclass
class ServerIO:
    run: RunFn
    capture: CaptureFn
    prompt: Callable[[str], str]
    # Is <name> already a tagged node on the tailnet? True/False, or None when unknown (DR-10).
    # Optional: test doubles that do not define it make `up` use the secrets rule alone.
    tailnet_node: Optional[Callable[[str, str, str, str], Optional[bool]]] = None


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

    def tailnet_node(name, client_id, client_secret, tailnet):
        return tailnet_mod.node_registered(name, client_id, client_secret, tailnet=tailnet or "-")

    return ServerIO(run=run, capture=capture, prompt=prompt, tailnet_node=tailnet_node)


MIN_CONSOLE_PASSWORD = 12


def console_password(config: config_mod.Config, source: secrets_mod.SecretSource, name: str) -> str:
    """The root password for the provider console from the secret source, or ``""`` (D-077). A bad value is refused, never applied."""
    key = config.console_password_secret(name)
    found = source.get(key)
    if not found.found:
        return ""
    value = found.value
    if len(value) < MIN_CONSOLE_PASSWORD or "\n" in value or "\r" in value:
        raise config_mod.ConfigError(
            f"{key} must be at least {MIN_CONSOLE_PASSWORD} characters on one line; it is the root password for the provider console"
        )
    return value


def managed_firewall(config, source, provider: str, name: str):
    """The provider-firewall function for this server when sealedstack can drive it through an API, else ``None``.

    Netcup: when the Netcup refresh token is in the secret source and the server has an ``external_id`` (D-074). Anything else
    keeps the manual steps (print the rule table, wait for the operator)."""
    if provider != "netcup":
        return None
    spec = (config.servers.get("servers") or {}).get(name) or {}
    if not spec.get("external_id") or not tofu_mod.optional(source, config, "netcup_refresh_token"):
        return None
    from . import netcupfw

    return lambda ssh, out, **kw: netcupfw.apply(config, dict(kw.get("env") or {}), name, ssh=ssh, source=source, out=out)


def ansible_env(env: Mapping[str, str], code_root: Path) -> dict:
    """``env`` plus the Ansible settings that point at the engine's own tree.

    ``ansible.cfg`` and its relative ``roles_path`` are only found when the working directory is the engine's tree, but
    the engine runs from the infra repo (``/infra``), so ``include_role`` could not find ``mesh_tailscale`` (first live
    bootstrap). Both are set explicitly unless the caller already chose them.
    """
    out = dict(env)
    out.setdefault("ANSIBLE_CONFIG", str(Path(code_root) / "ansible.cfg"))
    out.setdefault("ANSIBLE_ROLES_PATH", str(Path(code_root) / "ansible" / "roles"))
    return out


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
    generated: Optional[generated_mod.Generated] = None,
    sleep: Callable[[float], None] = time.sleep,
    firewall: Optional[Callable[..., bool]] = None,
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
    reject_unimplemented_method(config, name)

    root_password = ""
    ip = ""
    already_bootstrapped = False
    registered = _registered_on_tailnet(config, name, source, io, out)
    if creates_server(code_root, provider):
        print(f"==> provisioning {name} via {provider}", file=out)
        rc, stdout = io.capture([sys.executable, "-m", "sealedstack.compute", name, "apply"], env)
        if rc != 0:
            raise config_mod.ConfigError(f"compute apply failed for {name} (exit {rc})")
        ip = stdout.strip().splitlines()[-1] if stdout.strip() else ""
        # The provider-issued root password (e.g. Hetzner e-mails one when the server is
        # created without an SSH key) is read from the secret source like any other.
        found_pw = source.get(pw_key)
        root_password = found_pw.value if found_pw.found else ""
        already_bootstrapped = registered is True
    elif registered is True:
        # DR-10: the tailnet is the authority. A node named <name> with tag:server means the
        # bootstrap is done, whatever is left in the secret source.
        already_bootstrapped = True
        print(f"==> {name}: already a node on the tailnet; re-asserting the baseline over the mesh", file=out)
    elif not source.get(ip_key).found and not source.get(pw_key).found:
        if registered is False:
            raise config_mod.ConfigError(
                f"'{name}' is not on the tailnet and its bootstrap secrets are missing: add {ip_key} and {pw_key} "
                "to the secret source so `up` can do the first login (D-049)"
            )
        # The tailnet could not say (no devices scope, or no network): fall back to D-049.
        # Both bootstrap secrets are deleted after the first bootstrap, so their absence
        # means "already bootstrapped". Only re-assert the baseline over the mesh.
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
    drive_firewall = firewall if firewall is not None else managed_firewall(config, source, provider, name)
    if not already_bootstrapped and drive_firewall is not None:
        print(f"==> {name}: opening TCP 22 on the provider firewall for the first login", file=out)
        drive_firewall("open", out, env=env)
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
    # Generated keys live in the encrypted state (D-038): the Dokploy -> worker key
    # and the CI key. The CI key is authorised for the admin user next to the
    # operator's keys, so a runner on the tailnet can run the baseline over SSH.
    gen = generated or generated_mod.shared(config, env, source)
    state = gen.ensure()
    dokploy_ssh_public_key = str(state["dokploy_ssh_public_key"])
    admin_ssh_keys_json = json.dumps([*config.admin_ssh_keys(), str(state["ci_ssh_public_key"])])
    identity = _write_identity(str(state["ci_ssh_private_key"]))

    fd, vars_path = tempfile.mkstemp()
    os.close(fd)
    vars_file = Path(vars_path)
    known_hosts = ""
    try:
        if not already_bootstrapped:
            # D-064: with a pinned fingerprint, the first (password) login only goes to
            # a server that presents exactly that host key.
            known_hosts = _pinned_known_hosts(config, name, ip, io, env, out, sleep)
            write_vars(
                vars_file,
                {
                    "target_name": name,
                    "tailscale_auth_key": auth_key,
                    "admin_user": admin_user,
                    "admin_ssh_keys_json": admin_ssh_keys_json,
                    "admin_ssh_identity": identity,
                    "ansible_password": root_password or env.get("ROOT_PASSWORD", ""),
                    "bootstrap_known_hosts": known_hosts,
                },
            )
            bootstrap_args = ["-e", f"@{vars_file}"]
            if not (root_password or env.get("ROOT_PASSWORD")):
                # D-061: first login is by root password only. Never fall back to an
                # interactive prompt: in CI it would hang the job, and a missing secret
                # is something the user can fix.
                raise config_mod.ConfigError(missing_password_message(name, pw_key, creates_server(code_root, provider)))

            print(f"==> bootstrap {name}", file=out)
            rc = io.run(
                ["ansible-playbook", "-i", f"{ip},", "-u", "root", str(code_root / "ansible" / "playbooks" / "bootstrap.yml"), *bootstrap_args],
                ansible_env(env, code_root),
            )
            if rc != 0:
                raise config_mod.ConfigError(f"bootstrap failed for {name} (exit {rc})")

        write_vars(
            vars_file,
            {
                "dokploy_ssh_public_key": dokploy_ssh_public_key,
                "admin_ssh_keys_json": admin_ssh_keys_json,
                "ansible_ssh_private_key_file": identity,
                "root_console_password": console_password(config, source, name),
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
            ansible_env(env, code_root),
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
        Path(identity).unlink(missing_ok=True)
        if known_hosts:
            Path(known_hosts).unlink(missing_ok=True)

    if already_bootstrapped:
        print(_checklist(name), file=out)
        return f"sealedstack: server '{name}' is up"
    firewall_mode = registry_value(code_root, "compute", provider, "firewall", "manual")
    if drive_firewall is not None:
        print(f"==> {name}: applying the provider firewall rule table and closing TCP 22", file=out)
        drive_firewall("closed", out, env=env)
        print(_checklist(name), file=out)
        return f"sealedstack: server '{name}' is up"
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
    generated: Optional[generated_mod.Generated] = None,
) -> int:
    """Re-run ``site.yml`` over the mesh (drift correction, upgrades)."""
    env = dict(env or os.environ)
    io = io or _default_io()
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    import tempfile

    # D-055: the admin user has passwordless sudo, so no password is needed here;
    # the public keys (operator + CI) are re-asserted, and the CI key is the login.
    state = (generated or generated_mod.shared(config, env, source)).ensure()
    identity = _write_identity(str(state["ci_ssh_private_key"]))
    fd, vars_path = tempfile.mkstemp()
    os.close(fd)
    vars_file = Path(vars_path)
    try:
        write_vars(
            vars_file,
            {
                "admin_ssh_keys_json": json.dumps([*config.admin_ssh_keys(), str(state["ci_ssh_public_key"])]),
                "dokploy_ssh_public_key": str(state["dokploy_ssh_public_key"]),
                "ansible_ssh_private_key_file": identity,
                "root_console_password": console_password(config, source, name) if name else "",
            },
        )
        argv = ["ansible-playbook", "-i", str(Path(config.code_root) / "ansible" / "inventory.py")]
        if name:
            argv += ["--limit", name]
        argv += ["-e", f"@{vars_file}", str(Path(config.code_root) / "ansible" / "playbooks" / "site.yml")]
        return io.run(argv, ansible_env(env, Path(config.code_root)))
    finally:
        vars_file.unlink(missing_ok=True)
        Path(identity).unlink(missing_ok=True)


METHOD_KEY_MESSAGE = "bootstrap.method: key is not implemented yet; use password (D-061)"


def reject_unimplemented_method(config: config_mod.Config, name: str) -> None:
    """``bootstrap.method: key`` is in the schema for later; today it is refused (D-061)."""
    spec = (config.servers.get("servers") or {}).get(name) or {}
    if str((spec.get("bootstrap") or {}).get("method") or "password") == "key":
        raise config_mod.ConfigError(f"server '{name}': {METHOD_KEY_MESSAGE}")


def missing_password_message(name: str, pw_key: str, provider_creates: bool) -> str:
    extra = (
        " (Hetzner e-mails a root password when a server is created without an SSH key)" if provider_creates else ""
    )
    return (
        f"no root password for '{name}': the first login uses the provider-issued root password (D-061) and "
        f"{pw_key} is not in the secret source{extra}. Add {pw_key}; sealedstack never prompts for it, so a CI run "
        "cannot hang waiting for one."
    )


def _registered_on_tailnet(config, name: str, source, io: ServerIO, out) -> Optional[bool]:
    """Ask the tailnet whether ``name`` is already a tagged node (None = cannot tell)."""
    check = getattr(io, "tailnet_node", None)
    if check is None:
        return None
    client_id = source.get("TAILSCALE_OAUTH_CLIENT_ID")
    client_secret = source.get("TAILSCALE_OAUTH_CLIENT_SECRET")
    if not (client_id.found and client_secret.found):
        return None
    answer = check(name, client_id.value, client_secret.value, "-")  # "-" = the OAuth client's own tailnet, as for the auth keys
    if answer is None:
        print(f"==> {name}: could not ask the tailnet whether it is bootstrapped (the OAuth client may lack the devices scope); using the secrets rule", file=out)
    return answer


def _pinned_known_hosts(config, name: str, ip: str, io: ServerIO, env, out, sleep) -> str:
    """Path of a 0600 ``known_hosts`` with the pinned key, or ``""`` when no fingerprint is configured."""
    wanted = hostkey_mod.configured(config, name)
    if not wanted:
        print(
            f"==> {name}: no bootstrap.host_key_fingerprint; the first login will not verify the server's host key (D-064)",
            file=out,
        )
        return ""
    hostkey_mod.normalise(wanted)  # a malformed fingerprint fails before anything is sent
    scanned = ""
    for attempt in range(4):  # a server that was just created may not answer yet
        rc, scanned = io.capture(hostkey_mod.scan_command(ip), env)
        if rc == 0 and hostkey_mod.parse_scan(scanned):
            break
        if attempt < 3:
            sleep(5)
    line = hostkey_mod.known_hosts_line(scanned, ip, wanted, name=f"'{name}'")
    import tempfile

    fd, path = tempfile.mkstemp(prefix="sealedstack-known-hosts-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(line)
    os.chmod(path, 0o600)
    print(f"==> {name}: SSH host key matches the pinned fingerprint; host-key checking is on for the first login", file=out)
    return path


def _write_identity(private_key: str) -> str:
    """The CI SSH private key as a 0600 temp file (never in argv or the environment)."""
    import tempfile

    fd, path = tempfile.mkstemp(prefix="sealedstack-ci-key-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(private_key if private_key.endswith("\n") else private_key + "\n")
    os.chmod(path, 0o600)
    return path


def _capture_required(io: ServerIO, argv: list, env: Mapping[str, str]) -> str:
    rc, stdout = io.capture(argv, env)
    if rc != 0:
        raise config_mod.ConfigError(f"{argv[0]} failed (exit {rc})")
    return stdout.strip()


def _checklist(name: str) -> str:
    return (
        f"\nsealedstack: first-run checklist for '{name}'\n"
        "  1. Confirm the provider firewall rule table and close the temporary inbound TCP 22 window (docs/netcup-firewall.md).\n"
        "  2. Delete the bootstrap secrets (the server's IP and root password) from the secret source: they are no longer needed.\n"
        "  3. 'sealedstack up' creates the Dokploy admin and API key itself (kept in the encrypted state); run it again to continue.\n"
        "  4. Run 'sealedstack check' to gate every step above.\n"
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
