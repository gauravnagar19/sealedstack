"""``connector``: keep each app's tunnel connector fed with its token and running (D-073).

The edge stack creates the tunnel and its token; the platform stack creates a ``<app>-connector`` compose in Dokploy
that runs ``cloudflared`` (or ngrok). The token has to reach that container through the compose's ``env``. Leaving that
to the OpenTofu provider failed in the first live run: the provider's ``dokploy_compose`` shows an in-place change on
every apply and its update clears ``env``, while ``dokploy_environment_variables`` still believes the value is set. Every
deployment then started ``cloudflared`` with a blank ``TUNNEL_TOKEN`` and Cloudflare answered error 1033.

So the engine owns this field (one writer per Dokploy field, D-032). After each ``platform apply``:

1. read the plain edge outputs (``connector_tokens``, ``tunnel_ids``, ``ingress_type``);
2. make the compose's ``env`` carry the token, keeping every other line;
3. ask Cloudflare whether the tunnel has a live connection; a connected tunnel is never restarted (restoring ``env``
   alone is enough, the running container already has its token);
4. if it is not connected, deploy once and wait for the connection. When it still does not connect, say why it usually
   does not: the provider firewall must let Cloudflare's replies on UDP 7844 reach the server.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import config as config_mod
from . import deployapps
from . import dokploy as dokploy_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod
from .http import HttpClient, UrllibClient

CF_API = "https://api.cloudflare.com/client/v4"
CONNECT_TIMEOUT = 120
POLL_SECONDS = 5


class ConnectorError(config_mod.ConfigError):
    pass


def env_key(ingress_type: str) -> str:
    return "NGROK_AUTHTOKEN" if ingress_type == "ngrok" else "TUNNEL_TOKEN"


def with_variable(env_text: str, key: str, value: str) -> tuple[str, bool]:
    """``env_text`` with ``key=value`` set and every other line kept; True when something changed."""
    lines = [line for line in str(env_text or "").splitlines()]
    out, found, changed = [], False, False
    for line in lines:
        name = line.split("=", 1)[0].strip()
        if name == key and "=" in line:
            if found:  # a duplicate of the key: drop it
                changed = True
                continue
            found = True
            if line.split("=", 1)[1] != value:
                changed = True
            out.append(f"{key}={value}")
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
        changed = True
    return "\n".join(out), changed


def tunnel_connected(
    http: HttpClient, token: str, account_id: str, tunnel_id: str
) -> Optional[bool]:
    """True when Cloudflare lists a live connection, False when it lists none, None when it cannot say."""
    try:
        response = http.request(
            "GET", f"{CF_API}/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status != 200:
            return None
        result = (response.json() or {}).get("result") or {}
    except Exception:  # noqa: BLE001 - unknown, never a reason to fail on its own
        return None
    if result.get("connections") is not None:
        return len(result["connections"]) > 0
    return str(result.get("status") or "") in ("healthy", "degraded")


def find_connector_composes(client, app: str) -> list:
    """Every compose called ``<app>-connector`` (one per server)."""
    found = []
    for project in client.get("/api/project.all") or []:
        for environment in project.get("environments") or [{"compose": project.get("compose") or []}]:
            for compose in environment.get("compose") or []:
                if compose.get("name") == f"{app}-connector":
                    found.append(str(compose["composeId"]))
    return found


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    client=None,
    http: Optional[HttpClient] = None,
    sleep: Callable[[float], None] = time.sleep,
    out=None,
    outputs: Optional[dict] = None,
) -> int:
    out = out if out is not None else sys.stdout
    env = dict(env or os.environ)
    http = http or UrllibClient()
    if outputs is None:
        path = Path(config.code_root) / ".build" / "edge-outputs.json"
        outputs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    tokens = outputs.get("connector_tokens") or {}
    tunnel_ids = outputs.get("tunnel_ids") or {}
    ingress_type = str(outputs.get("ingress_type") or "cloudflare")
    tokens = {app: value for app, value in tokens.items() if value}
    if not tokens:
        print("connector: no tunnel connectors to maintain", file=out)
        return 0

    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    client = client or dokploy_mod.from_config(config, env, source)
    cf_token = ""
    if ingress_type != "ngrok":
        cf_token = tofu_mod.require(source, config, "CLOUDFLARE_API_TOKEN")
    account_id = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
    key = env_key(ingress_type)

    failures = []
    for app, token in sorted(tokens.items()):
        composes = find_connector_composes(client, app)
        if not composes:
            print(f"connector: {app}: no '{app}-connector' compose in Dokploy yet; nothing to do", file=out)
            continue
        changed = False
        for compose_id in composes:
            new_env, differs = with_variable(client.compose_env(compose_id), key, token)
            if differs:
                client.compose_update(compose_id, new_env)
                changed = True

        tunnel_id = str(tunnel_ids.get(app) or "")
        connected = (
            tunnel_connected(http, cf_token, account_id, tunnel_id) if (cf_token and tunnel_id) else None
        )
        if connected:
            print(f"connector: {app}: connected" + (" (env restored; no restart needed)" if changed else ""), file=out)
            continue

        print(f"connector: {app}: not connected; deploying the connector" + (" with its token" if changed else ""), file=out)
        for compose_id in composes:
            previous = deployapps.deploy_ids(client, compose_id)
            client.compose_deploy(compose_id)
            result = deployapps.poll_deploy(client, compose_id, previous, sleep=sleep)
            print(f"    {compose_id}: {result}", file=out)
        if not (cf_token and tunnel_id):
            continue  # ngrok, or no tunnel id to ask about
        waited = 0
        while waited < CONNECT_TIMEOUT:
            if tunnel_connected(http, cf_token, account_id, tunnel_id):
                print(f"connector: {app}: connected", file=out)
                break
            sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        else:
            failures.append(app)
            print(
                f"connector: {app}: the container is running but the tunnel did not connect within {CONNECT_TIMEOUT}s. "
                "The usual cause is the provider firewall: Cloudflare's replies reach cloudflared on UDP source port 7844 "
                "(see docs/netcup-firewall.md). Check `docker logs` of the connector on the server.",
                file=out,
            )
    return 1 if failures else 0


def main(argv: Optional[list] = None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        return run(config, env)
    except (config_mod.ConfigError, dokploy_mod.DokployError) as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
