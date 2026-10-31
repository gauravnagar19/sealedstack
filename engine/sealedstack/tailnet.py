"""Is a server already a node on the tailnet? (DR-10)

``up`` used to decide "this server is already bootstrapped" from the absence of
both bootstrap secrets. A first run that forgot both secrets was then taken for a
finished bootstrap and failed later, over the mesh, with a confusing error. The
tailnet is the real answer: a bootstrapped server is a node named ``<server>``
carrying ``tag:server``. This asks the Tailscale API with the OAuth client the
engine already uses (client-credentials flow). It returns ``None`` (unknown) for
anything else, e.g. a client without the devices-read scope, and the caller falls
back to the old rule.
"""
from __future__ import annotations

import base64
from typing import Optional

from .http import HttpClient, UrllibClient

API = "https://api.tailscale.com/api/v2"
TAG = "tag:server"


def node_registered(
    name: str,
    client_id: str,
    client_secret: str,
    *,
    tailnet: str = "-",
    http: Optional[HttpClient] = None,
) -> Optional[bool]:
    """True/False when the tailnet says whether ``name`` is a tagged node; None when it cannot say."""
    if not (name and client_id and client_secret):
        return None
    http = http or UrllibClient()
    try:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
        token_response = http.request(
            "POST", f"{API}/oauth/token",
            headers={"Authorization": f"Basic {basic}"}, form_body={"grant_type": "client_credentials"},
        )
        if token_response.status != 200:
            return None
        token = str((token_response.json() or {}).get("access_token") or "")
        if not token:
            return None
        devices = http.request("GET", f"{API}/tailnet/{tailnet}/devices", headers={"Authorization": f"Bearer {token}"})
        if devices.status != 200:
            return None  # e.g. 403: the client was not given the devices scope
        rows = (devices.json() or {}).get("devices")
    except Exception:  # noqa: BLE001 - unknown, never a reason to fail the run
        return None
    if not isinstance(rows, list):
        return None
    for device in rows:
        hostname = str(device.get("hostname") or "")
        fqdn = str(device.get("name") or "")
        if name in (hostname, fqdn.split(".", 1)[0]) and TAG in (device.get("tags") or []):
            return True
    return False
