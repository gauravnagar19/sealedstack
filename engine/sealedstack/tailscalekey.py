"""Mint a single-use, pre-authorised ``tag:server`` auth key (spec §8, D-047).

Uses the Tailscale **OAuth client** (client-credentials flow), never a long-lived
API key. Prints the key on stdout. Replaces ``scripts/tailscale-key.sh``.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping, Optional

API = "https://api.tailscale.com/api/v2"


def run(env: Optional[Mapping[str, str]] = None) -> str:
    env = dict(env or os.environ)
    client_id = env.get("TAILSCALE_OAUTH_CLIENT_ID", "")
    client_secret = env.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("TAILSCALE_OAUTH_CLIENT_ID/_SECRET must be set")
    tailnet = env.get("TAILSCALE_TAILNET", "-")
    # Defaults mint the server key `up` needs. A maintainer's local run (scripts/dev-local.sh) joins the tailnet as
    # `tag:ci` with an ephemeral key instead, the way the CI job does.
    tags = [t.strip() for t in env.get("TAILSCALE_KEY_TAGS", "tag:server").split(",") if t.strip()] or ["tag:server"]
    ephemeral = str(env.get("TAILSCALE_KEY_EPHEMERAL", "")).lower() in ("1", "true", "yes")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    token_request = urllib.request.Request(
        f"{API}/oauth/token",
        data=b"grant_type=client_credentials",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    with urllib.request.urlopen(token_request, timeout=30) as response:
        token = json.loads(response.read().decode("utf-8"))["access_token"]

    body = json.dumps(
        {
            "capabilities": {
                "devices": {
                    "create": {
                        "reusable": False,
                        "ephemeral": ephemeral,
                        "preauthorized": True,
                        "tags": tags,
                    }
                }
            },
            "expirySeconds": 3600,
        }
    ).encode("utf-8")
    key_request = urllib.request.Request(
        f"{API}/tailnet/{urllib.parse.quote(tailnet, safe='')}/keys",
        data=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(key_request, timeout=30) as response:
        return str(json.loads(response.read().decode("utf-8"))["key"])


def main(argv: Optional[list[str]] = None) -> int:
    try:
        print(run(dict(os.environ)))
    except urllib.error.HTTPError as exc:
        # Tailscale says why in the body (for example which tag the OAuth client may not use); show it, never the key.
        detail = exc.read().decode("utf-8", "replace").strip().replace("\n", " ")[:300]
        print(f"sealedstack: tailscale-key: HTTP {exc.code} {exc.reason}: {detail}", file=sys.stderr)
        return 1
    except (RuntimeError, urllib.error.URLError, KeyError) as exc:
        print(f"sealedstack: tailscale-key: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
