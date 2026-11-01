"""``reinstall``: wipe a Netcup server and install a fresh OS through the SCP REST API (D-070).

Netcup gives sealedstack no way to create a server, but its Server Control Panel REST API can
format a server's disk and install an image. This runs that from CI, behind the same approval
gate as ``up``, so a rebuild-from-nothing test needs no clicking in the provider panel.

What it does, in order:

1. read the secrets: the Netcup refresh token and the server's root password (both from the
   secret source; sealedstack never writes to it, D-038, so you create the password first);
2. exchange the refresh token for an access token, find the server (``external_id``), its disk
   and the image flavour, and print exactly what is about to be wiped;
3. refuse unless ``--confirm <name>`` repeats the server name;
4. start the image install with a first-boot script that sets that root password, then wait
   for Netcup's task to finish;
5. forget what ``up`` remembered about the old install (the tailnet node and the saved phases),
   so the next ``up`` bootstraps the new one from the root password (D-061).

Nothing here prints a token, the password or the public IP.
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from . import config as config_mod
from . import hostkey as hostkey_mod
from . import secrets as secrets_mod
from . import tailnet as tailnet_mod
from . import tofu as tofu_mod
from .http import HttpClient, UrllibClient

API = "https://www.servercontrolpanel.de/scp-core/api/v1"
TOKEN_URL = "https://www.servercontrolpanel.de/realms/scp/protocol/openid-connect/token"
CLIENT_ID = "scp"
DEFAULT_IMAGE = "ubuntu-24.04"
BOOTSTRAP_DROPIN = "/etc/ssh/sshd_config.d/00-sealedstack-bootstrap.conf"  # removed by ansible/playbooks/bootstrap.yml
DOCS = "docs/netcup-reinstall.md"

POLL_SECONDS = 10
TASK_TIMEOUT = 30 * 60
SSH_TIMEOUT = 10 * 60
FAILED_STATES = {"ERROR", "CANCELED", "WAITING_FOR_CANCEL", "ROLLBACK"}

PASSWORD_RE = re.compile(r"^[!-~]{12,}$")  # printable ASCII, no spaces, at least 12


class ReinstallError(config_mod.ConfigError):
    pass


@dataclass
class Plan:
    name: str
    server_id: int
    server_label: str
    disk: str
    flavour_id: int
    flavour_label: str


# --------------------------------------------------------------------------- #
# the SCP API
# --------------------------------------------------------------------------- #
def login(http: HttpClient, refresh_token: str) -> str:
    """An access token for ``refresh_token`` (the long-lived offline token, kept as a secret)."""
    try:
        response = http.request(
            "POST", TOKEN_URL,
            form_body={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token},
        )
    except Exception as exc:  # noqa: BLE001 - say what failed, never the token
        raise ReinstallError(f"could not reach Netcup's login service: {type(exc).__name__}") from exc
    token = ""
    if response.status == 200:
        token = str((response.json() or {}).get("access_token") or "")
    if not token:
        raise ReinstallError(
            f"Netcup refused the refresh token (HTTP {response.status}). It expires after about 30 days "
            f"without use; create a new one and update the secret ({DOCS}#renewing-the-token)"
        )
    return token


def _call(http: HttpClient, token: str, method: str, path: str, **kwargs):
    try:
        return http.request(method, f"{API}{path}", headers={"Authorization": f"Bearer {token}"}, **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise ReinstallError(f"could not reach the Netcup API ({method} {path}): {type(exc).__name__}") from exc


def _json(response, what: str):
    if response.status not in (200, 202):
        detail = response.text.strip().replace("\n", " ")[:200]
        raise ReinstallError(f"Netcup API: {what} failed (HTTP {response.status}) {detail}".rstrip())
    return response.json()


def find_server(http: HttpClient, token: str, external_id: str) -> dict:
    """The server whose id or name (``v22…``) equals ``external_id``, with its live info."""
    listing = _json(_call(http, token, "GET", "/servers"), "listing servers") or []
    wanted = str(external_id)
    match = next((s for s in listing if wanted in (str(s.get("id")), str(s.get("name")))), None)
    if match is None:
        raise ReinstallError(
            f"no server '{wanted}' in this Netcup account (servers.yaml external_id): it must be the id or the "
            "v22… name shown in the Server Control Panel"
        )
    return _json(_call(http, token, "GET", f"/servers/{match['id']}"), "reading the server") or {}


def pick_disk(server: dict, wanted: str) -> str:
    disks = [str(d.get("dev")) for d in ((server.get("serverLiveInfo") or {}).get("disks") or []) if d.get("dev")]
    if wanted:
        if wanted not in disks:
            raise ReinstallError(f"the server has no disk '{wanted}' (it has: {', '.join(disks) or 'none'})")
        return wanted
    if len(disks) == 1:
        return disks[0]
    if not disks:
        raise ReinstallError("Netcup reports no disk for this server")
    raise ReinstallError(f"the server has several disks ({', '.join(disks)}); pass --disk to choose the one to wipe")


def _image_name(flavour: dict) -> str:
    return str((flavour.get("image") or {}).get("name") or "")


def _flavour_label(flavour: dict) -> str:
    """``Ubuntu 24.04, Minimal (id 123)``: Netcup's flavour is only the variant, the OS is on its ``image``."""
    parts = [p for p in (_image_name(flavour), str(flavour.get("name") or "")) if p]
    return f"{', '.join(parts)} (id {flavour.get('id')})"


def _words(flavour: dict) -> list:
    return _image_name(flavour).lower().split()


def pick_flavour(
    flavours: Sequence[dict],
    wanted: str,
    forced_id: str = "",
    *,
    uefi: Optional[bool] = None,
    arch: str = "",
) -> dict:
    """Choose the image flavour: an explicit id, an exact alias, or the one that fits the OS and the server.

    Netcup lists one flavour per OS build with the OS in ``image.name`` (``Debian 13.7.0 UEFI amd64``) and
    the variant in the flavour name (``Minimal``). ``debian-13`` is matched against the image name, then
    narrowed in this order, each step only when it leaves something: a plain OS image (``Ubuntu cloudimg``
    and ``Ubuntu openclaw`` are other products), the server's boot mode (UEFI or BIOS, from the server
    itself), its architecture, and the ``minimal`` variant. Anything still ambiguous is listed.
    """
    if forced_id:
        for flavour in flavours:
            if str(flavour.get("id")) == str(forced_id):
                return flavour
        raise ReinstallError(f"Netcup offers no image flavour with id {forced_id} for this server")
    wanted = wanted or DEFAULT_IMAGE
    for flavour in flavours:
        if str(flavour.get("alias") or "").lower() == wanted.lower():
            return flavour
    tokens = [t for t in re.split(r"[-\s]+", wanted.lower()) if t]

    def matches(flavour: dict) -> bool:
        haystack = f"{_image_name(flavour)} {flavour.get('alias')} {flavour.get('name')}".lower()
        return all(token in haystack for token in tokens)

    def plain(flavour: dict) -> bool:
        words = _words(flavour)
        return len(words) >= 2 and len(tokens) >= 2 and words[0] == tokens[0] and words[1].startswith(tokens[1])

    found = [f for f in flavours if matches(f)]
    if uefi is not None and found:
        # A BIOS image on a UEFI server (or the reverse) may not boot, so the boot mode is required, not preferred.
        want_mode = "uefi" if uefi else "bios"
        same_mode = [f for f in found if want_mode in _words(f)]
        if not same_mode:
            raise ReinstallError(
                f"Netcup offers no {want_mode.upper()} image for '{wanted}' and this server boots {want_mode.upper()}. "
                "Pick another OS, or change the server's boot mode in the Server Control Panel first. Matching images:\n"
                + "\n".join(f"  - {_flavour_label(f)}" for f in found[:20])
            )
        found = same_mode
    steps = [plain]
    if arch:
        steps.append(lambda f: arch.lower() in _words(f))
    steps.append(lambda f: str(f.get("alias") or f.get("name") or "").lower() == "minimal")
    for keep in steps:
        if len(found) > 1:
            narrowed = [f for f in found if keep(f)]
            if narrowed:
                found = narrowed
    if len(found) == 1:
        return found[0]
    listing = "\n".join(f"  - {_flavour_label(f)}" for f in (found or flavours)[:40])
    why = "matches several images" if found else "matches no image"
    raise ReinstallError(
        f"image '{wanted}' {why}; pass --flavour <id> or set `os` in servers.yaml. Available:\n{listing}"
    )


def root_script(password: str) -> str:
    """The first-boot script that makes ``root`` reachable by this password, for the bootstrap only.

    The password is shipped base64-encoded so no quoting can alter it; the bootstrap play
    (``site.yml`` baseline) turns root login and password authentication off again (D-061).
    """
    encoded = base64.b64encode(f"root:{password}".encode("utf-8")).decode("ascii")
    return (
        "#!/bin/bash\n"
        "set -eu\n"
        f"echo '{encoded}' | base64 -d | chpasswd\n"
        "install -d -m 755 /etc/ssh/sshd_config.d\n"
        f"printf 'PermitRootLogin yes\\nPasswordAuthentication yes\\n' > {BOOTSTRAP_DROPIN}\n"
        "systemctl restart ssh 2>/dev/null || systemctl restart sshd\n"
    )


def wait_for_task(http: HttpClient, token: str, uuid: str, *, sleep: Callable[[float], None], out) -> None:
    waited = 0
    while True:
        task = _json(_call(http, token, "GET", f"/tasks/{uuid}"), "reading the install task") or {}
        state = str(task.get("state") or "")
        if state == "FINISHED":
            return
        if state in FAILED_STATES:
            detail = task.get("message") or (task.get("responseError") or {}).get("message") or "no detail given"
            raise ReinstallError(f"Netcup's image install ended in state {state}: {detail}")
        progress = (task.get("taskProgress") or {}).get("progressInPercent")
        print(f"==> install {state.lower() or 'pending'}" + (f" ({int(progress)}%)" if progress is not None else ""), file=out, flush=True)
        if waited >= TASK_TIMEOUT:
            raise ReinstallError(f"the image install did not finish within {TASK_TIMEOUT // 60} minutes; check the task in the Server Control Panel")
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS


# --------------------------------------------------------------------------- #
# the command
# --------------------------------------------------------------------------- #
def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sealedstack reinstall", add_help=True)
    parser.add_argument("name", nargs="?", help="the server (as in servers.yaml)")
    parser.add_argument("--name", dest="name_flag", help="the server (as in servers.yaml)")
    parser.add_argument("--confirm", default="", help="repeat the server name to confirm the wipe")
    parser.add_argument("--dry-run", action="store_true", help="show what would be wiped and stop")
    parser.add_argument("--flavour", default="", help="Netcup image flavour id (overrides `image`)")
    parser.add_argument("--disk", default="", help="the disk to wipe, if the server has more than one")
    return parser.parse_args(list(argv))


def _wait_for_ssh(ip: str, probe: Callable[[str, int, float], bool], sleep, out) -> bool:
    waited = 0
    while waited < SSH_TIMEOUT:
        if probe(ip, 22, 5.0):
            return True
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS
    return False


def _default_probe(host: str, port: int, timeout: float) -> bool:
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def run(
    config: config_mod.Config,
    env: Mapping[str, str],
    argv: Sequence[str],
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    http: Optional[HttpClient] = None,
    out=None,
    sleep: Callable[[float], None] = time.sleep,
    forget_progress: Optional[Callable[[], None]] = None,
    remove_node: Optional[Callable[[str, str, str], Optional[int]]] = None,
    tcp_probe: Callable[[str, int, float], bool] = _default_probe,
) -> int:
    out = out if out is not None else sys.stdout
    try:
        return _run(config, env, argv, source, http or UrllibClient(), out, sleep, forget_progress, remove_node, tcp_probe)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=out)
        return 1


def _run(config, env, argv, source, http, out, sleep, forget_progress, remove_node, tcp_probe) -> int:
    args = parse_args(argv)
    name = args.name_flag or args.name or ""
    if not name:
        raise ReinstallError("usage: reinstall --name <server> --confirm <server> [--dry-run]")
    spec = (config.servers.get("servers") or {}).get(name)
    if spec is None:
        raise ReinstallError(f"no server '{name}' in config/servers.yaml")
    if str(spec.get("provider") or "") != "netcup":
        raise ReinstallError(
            f"server '{name}' uses provider '{spec.get('provider')}': reinstall is only for Netcup servers "
            "(other providers create and destroy servers through OpenTofu)"
        )
    external_id = str(spec.get("external_id") or "")
    if not external_id:
        raise ReinstallError(f"server '{name}' has no external_id in servers.yaml: set it to the id or v22… name from the Server Control Panel")

    source = source or secrets_mod.build_source(config.stack, config.config_dir, dict(env))
    refresh_token = tofu_mod.require(source, config, "netcup_refresh_token")
    _, password_key = config.bootstrap_secrets(name)
    found = source.get(password_key)
    if not found.found:
        raise ReinstallError(
            f"no root password for '{name}': add {password_key} to the secret source first (at least 12 printable "
            "characters, no spaces). sealedstack cannot write to the secret source, so you choose it; the "
            "new install gets exactly this password for the first login (D-061)"
        )
    root_password = found.value
    if not PASSWORD_RE.match(root_password):
        raise ReinstallError(f"the root password in {password_key} must be at least 12 printable characters with no spaces or line breaks")

    token = login(http, refresh_token)
    server = find_server(http, token, external_id)
    flavours = _json(_call(http, token, "GET", f"/servers/{server['id']}/imageflavours"), "listing images") or []
    live = server.get("serverLiveInfo") or {}
    flavour = pick_flavour(
        flavours, str(spec.get("os") or spec.get("image") or ""), args.flavour,
        uefi=live.get("uefi") if isinstance(live.get("uefi"), bool) else None,
        arch=str(server.get("architecture") or ""),
    )
    plan = Plan(
        name=name,
        server_id=int(server["id"]),
        server_label=str(server.get("name") or server["id"]),
        disk=pick_disk(server, args.disk),
        flavour_id=int(flavour["id"]),
        flavour_label=_flavour_label(flavour),
    )

    print(
        f"sealedstack reinstall: {name} (Netcup {plan.server_label})\n"
        f"  will ERASE disk '{plan.disk}' and install {plan.flavour_label}\n"
        "  every container, volume and database on this server is lost",
        file=out,
    )
    if args.dry_run:
        print("dry run: nothing was changed", file=out)
        return 0
    if args.confirm != name:
        print(f"refusing to wipe '{name}': repeat the name to confirm (--confirm {name})", file=out)
        return 1

    body = {
        "imageFlavourId": plan.flavour_id,
        "diskName": plan.disk,
        "rootPartitionFullDiskSize": True,
        "hostname": name,
        "sshPasswordAuthentication": True,
        "customScript": root_script(root_password),
    }
    started = _json(_call(http, token, "POST", f"/servers/{plan.server_id}/image", json_body=body), "starting the image install") or {}
    uuid = str(started.get("uuid") or "")
    if not uuid:
        raise ReinstallError("Netcup accepted the install but returned no task id")
    print(f"==> install started (task {uuid[:8]}); this takes a few minutes", file=out, flush=True)
    wait_for_task(http, token, uuid, sleep=sleep, out=out)
    print("==> image installed", file=out)

    _forget_old_install(config, name, source, out, forget_progress, remove_node)
    if hostkey_mod.configured(config, name):
        print(
            f"note: servers.yaml pins bootstrap.host_key_fingerprint for '{name}', but the new install has a new host key. "
            "Replace it with the fingerprint the Server Control Panel now shows (or remove the line) before running `up`.",
            file=out,
        )
    ip = _secret_value(source, config.bootstrap_secrets(name)[0])
    if ip:
        if _wait_for_ssh(ip, tcp_probe, sleep, out):
            print("==> port 22 answers", file=out)
        else:
            print("warning: port 22 did not answer in 10 minutes. Open TCP 22 in the Netcup firewall (docs/netcup-firewall.md) and check the server in the panel.", file=out)
    print(f"done. Next: run `sealedstack up` (it bootstraps '{name}' with the root password from the secret source).", file=out)
    return 0


def _secret_value(source: secrets_mod.SecretSource, key: str) -> str:
    result = source.get(key)
    return result.value if result.found else ""


def _forget_old_install(config, name, source, out, forget_progress, remove_node) -> None:
    client_id = source.get("TAILSCALE_OAUTH_CLIENT_ID")
    client_secret = source.get("TAILSCALE_OAUTH_CLIENT_SECRET")
    if remove_node is None:
        remove_node = lambda n, cid, sec: tailnet_mod.remove_node(n, cid, sec)  # noqa: E731
    if client_id.found and client_secret.found:
        removed = remove_node(name, client_id.value, client_secret.value)
        if removed is None:
            print(
                f"warning: could not remove the old tailnet node '{name}' (the OAuth client needs the devices:core scope). "
                "Delete it in the Tailscale admin console, or `up` will think the new install is already bootstrapped.",
                file=out,
            )
        elif removed:
            print(f"==> removed the old tailnet node '{name}'", file=out)
    else:
        print(f"warning: no Tailscale credentials to remove the old node '{name}'; delete it in the admin console before `up`.", file=out)
    if forget_progress is not None:
        forget_progress()
        print("==> cleared the saved `up` progress", file=out)
