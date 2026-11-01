#!/usr/bin/env python3
"""Ask Tailscale whether the merged policy in .build/tailscale-policy.json is valid, without applying it (D-071).

Maintainers' check for a policy change (reads TAILSCALE_OAUTH_CLIENT_ID / _SECRET from the environment). Prints
Tailscale's verdict; exits 0 when the policy and its tests are accepted.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.tailscale.com/api/v2"


def call(url: str, *, data: bytes, headers: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def main() -> int:
    code = Path(os.environ.get("SEALEDSTACK_CODE", "."))
    policy = (code / ".build" / "tailscale-policy.json").read_text(encoding="utf-8")
    client_id, secret = os.environ.get("TAILSCALE_OAUTH_CLIENT_ID", ""), os.environ.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
    if not (client_id and secret):
        print("policy-validate: TAILSCALE_OAUTH_CLIENT_ID/_SECRET are not set", file=sys.stderr)
        return 2
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    status, body = call(f"{API}/oauth/token", data=b"grant_type=client_credentials", headers={"Authorization": f"Basic {basic}"})
    if status != 200:
        print(f"policy-validate: OAuth token request failed (HTTP {status})", file=sys.stderr)
        return 2
    token = json.loads(body)["access_token"]
    status, body = call(f"{API}/tailnet/-/acl/validate", data=policy.encode(),
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    print(f"Tailscale answered HTTP {status}: {body.strip() or 'the policy and its tests are valid'}")
    return 0 if status == 200 and not body.strip() else 1


if __name__ == "__main__":
    raise SystemExit(main())
