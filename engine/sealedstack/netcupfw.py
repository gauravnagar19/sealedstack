"""Netcup provider firewall through the SCP REST API (D-074).

Netcup's firewall is applied outside the server, per network interface, from user policies. sealedstack keeps one policy
of its own, ``sealedstack``, holding the rule table of ``docs/netcup-firewall.md``, and attaches only that policy (plus
Netcup's own copied ones, e.g. the SMTP block) to the server's interface. The only thing that changes between the phases
of a server's life is TCP 22:

* ``open``: before the first login of a bootstrap (``up``, ``reinstall``), because the root password login goes to the
  public address;
* ``closed``: after the bootstrap, when the server is reached over the tailnet only.

The first live run showed why this should not be left to hand: the policy someone had typed in lacked the
``UDP src 7844`` reply rule, so ``cloudflared`` could not reach Cloudflare (error 1033), and it still opened TCP 80 and
443 on the provider side.

``--dry-run`` prints what would change and touches nothing. Tokens never reach the output.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from typing import Callable, Mapping, Optional, Sequence

from . import config as config_mod
from . import reinstall as reinstall_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod
from .http import HttpClient, UrllibClient

POLICY_NAME = "sealedstack"
POLICY_DESCRIPTION = "managed by sealedstack: the rule table of docs/netcup-firewall.md"


class FirewallError(config_mod.ConfigError):
    pass


def rule(direction: str, protocol: str, action: str, *, src: str = "", dst: str = "", note: str = "") -> dict:
    out = {"direction": direction, "protocol": protocol, "action": action, "description": note}
    if src:
        out["sourcePorts"] = src
    if dst:
        out["destinationPorts"] = dst
    return out


def desired_rules(*, ssh_open: bool) -> list:
    """The rule table (docs/netcup-firewall.md); order matters, the drops come last."""
    rules = [
        rule("INGRESS", "UDP", "ACCEPT", src="53", note="DNS replies"),
        rule("INGRESS", "UDP", "ACCEPT", src="123", note="NTP replies"),
        rule("INGRESS", "UDP", "ACCEPT", src="7844", note="cloudflared QUIC replies"),
        rule("INGRESS", "UDP", "ACCEPT", dst="41641", note="Tailscale direct path"),
        rule("INGRESS", "ICMP", "ACCEPT", note="path MTU and ping"),
        rule("INGRESS", "ICMPv6", "ACCEPT", note="IPv6 neighbour discovery and ping"),
    ]
    if ssh_open:
        rules.append(rule("INGRESS", "TCP", "ACCEPT", dst="22", note="temporary: first SSH login of a bootstrap"))
    rules += [
        rule("INGRESS", "TCP", "DROP", note="everything else"),
        rule("INGRESS", "UDP", "DROP", note="everything else"),
    ]
    return rules


def _signature(item: Mapping) -> tuple:
    return (
        str(item.get("direction")), str(item.get("protocol")), str(item.get("action")),
        str(item.get("sourcePorts") or ""), str(item.get("destinationPorts") or ""),
    )


def same_rules(current: Sequence[Mapping], wanted: Sequence[Mapping]) -> bool:
    return [_signature(r) for r in current] == [_signature(r) for r in wanted]


def user_id(access_token: str) -> str:
    """The SCP user id is the ``id`` claim of the access token (it is not the customer number)."""
    try:
        payload = access_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception as exc:  # noqa: BLE001
        raise FirewallError("could not read the SCP user id from the access token") from exc
    uid = str(claims.get("id") or "")
    if not uid:
        raise FirewallError("the SCP access token carries no user id")
    return uid


def interface_mac(server: Mapping) -> str:
    interfaces = (server.get("serverLiveInfo") or {}).get("interfaces") or []
    with_ip = [i for i in interfaces if i.get("mac") and (i.get("ipv4Addresses") or i.get("ipv6Addresses"))]
    chosen = with_ip or [i for i in interfaces if i.get("mac")]
    if not chosen:
        raise FirewallError("Netcup reports no network interface for this server")
    return str(chosen[0]["mac"])


def _ids(items: Optional[Sequence]) -> list:
    return [{"id": i["id"]} if isinstance(i, dict) else {"id": i} for i in (items or [])]


def plan(
    http: HttpClient, access: str, server: Mapping, *, ssh_open: bool
) -> dict:
    """Read what exists and decide what to do; no writes."""
    uid = user_id(access)
    mac = interface_mac(server)
    sid = server["id"]
    current = reinstall_mod._json(reinstall_mod._call(http, access, "GET", f"/servers/{sid}/interfaces/{mac}/firewall"), "reading the firewall") or {}
    policies = reinstall_mod._json(reinstall_mod._call(http, access, "GET", f"/users/{uid}/firewall-policies"), "listing firewall policies") or []
    ours = next((p for p in policies if p.get("name") == POLICY_NAME), None)
    ours_rules = []
    if ours:
        detail = reinstall_mod._json(reinstall_mod._call(http, access, "GET", f"/users/{uid}/firewall-policies/{ours['id']}"), "reading the sealedstack policy") or {}
        ours_rules = detail.get("rules") or []
    wanted = desired_rules(ssh_open=ssh_open)
    attached = [p for p in (current.get("userPolicies") or [])]
    attached_ids = [p.get("id") if isinstance(p, dict) else p for p in attached]
    others = [p for p in attached if (p.get("id") if isinstance(p, dict) else p) != (ours or {}).get("id")]
    return {
        "uid": uid, "mac": mac, "sid": sid, "wanted": wanted, "ours": ours, "ours_rules": ours_rules,
        "policy_current": bool(ours) and same_rules(ours_rules, wanted),
        "attached_ids": attached_ids, "others": others, "copied": _ids(current.get("copiedPolicies")),
        "active": bool(current.get("active")),
    }


def describe(state: Mapping, *, ssh_open: bool) -> str:
    lines = [f"  TCP 22: {'OPEN (temporary)' if ssh_open else 'closed'}"]
    lines.append(f"  policy '{POLICY_NAME}': " + ("up to date" if state["policy_current"] else ("will be updated" if state["ours"] else "will be created")))
    if state["others"]:
        names = ", ".join(str(p.get("name") or p.get("id")) if isinstance(p, dict) else str(p) for p in state["others"])
        lines.append(f"  detached from the server: {names} (not deleted)")
    if not state["active"]:
        lines.append("  the firewall is currently inactive; it will be activated")
    return "\n".join(lines)


def apply(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    name: str = "",
    *,
    ssh: str = "closed",
    dry_run: bool = False,
    source: Optional[secrets_mod.SecretSource] = None,
    http: Optional[HttpClient] = None,
    out=None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Make the server's Netcup firewall match the rule table. Returns True when anything was (or would be) changed."""
    out = out if out is not None else sys.stdout
    env = dict(env or os.environ)
    if ssh not in ("open", "closed"):
        raise FirewallError("ssh must be 'open' or 'closed'")
    spec = (config.servers.get("servers") or {}).get(name)
    if spec is None:
        raise FirewallError(f"no server '{name}' in config/servers.yaml")
    if str(spec.get("provider") or "") != "netcup":
        raise FirewallError(f"server '{name}' is not a Netcup server")
    external_id = str(spec.get("external_id") or "")
    if not external_id:
        raise FirewallError(f"server '{name}' has no external_id in servers.yaml (the id or v22… name in the Server Control Panel)")
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    http = http or UrllibClient()
    access = reinstall_mod.login(http, tofu_mod.require(source, config, "netcup_refresh_token"))
    server = reinstall_mod.find_server(http, access, external_id)
    ssh_open = ssh == "open"
    state = plan(http, access, server, ssh_open=ssh_open)

    already = state["policy_current"] and not state["others"] and state["active"] and state["ours"] and state["ours"]["id"] in state["attached_ids"]
    print(f"netcup firewall for {name}:", file=out)
    print(describe(state, ssh_open=ssh_open), file=out)
    if already:
        print("  nothing to change", file=out)
        return False
    if dry_run:
        print("dry run: nothing was changed", file=out)
        return True

    body = {"name": POLICY_NAME, "description": POLICY_DESCRIPTION, "rules": state["wanted"]}
    if state["ours"]:
        policy_id = state["ours"]["id"]
        if not state["policy_current"]:
            reinstall_mod._json(reinstall_mod._call(http, access, "PUT", f"/users/{state['uid']}/firewall-policies/{policy_id}", json_body=body), "updating the sealedstack policy")
    else:
        created = reinstall_mod._json(reinstall_mod._call(http, access, "POST", f"/users/{state['uid']}/firewall-policies", json_body=body), "creating the sealedstack policy") or {}
        policy_id = created.get("id")
        if policy_id is None:
            raise FirewallError("Netcup created the policy but returned no id")
    answer = reinstall_mod._call(
        http, access, "PUT", f"/servers/{state['sid']}/interfaces/{state['mac']}/firewall",
        json_body={"copiedPolicies": state["copied"], "userPolicies": [{"id": policy_id}], "active": True},
    )
    started = reinstall_mod._json(answer, "attaching the firewall policy") or {}
    uuid = str(started.get("uuid") or "") if isinstance(started, dict) else ""
    if uuid:
        reinstall_mod.wait_for_task(http, access, uuid, sleep=sleep, out=out)
    print("  applied", file=out)
    return True


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sealedstack netcup firewall")
    parser.add_argument("--name", required=True, help="the server (as in servers.yaml)")
    parser.add_argument("--ssh", choices=("open", "closed"), default="closed", help="TCP 22 on the provider firewall")
    parser.add_argument("--dry-run", action="store_true", help="show what would change and stop")
    args = parser.parse_args(list(argv if argv is not None else sys.argv[1:]))
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        apply(config, env, args.name, ssh=args.ssh, dry_run=args.dry_run)
        return 0
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
