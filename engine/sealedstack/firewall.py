"""Apply a provider firewall through a REST API, driven by a spec file (spec §4).

Used by the ``compute_netcup`` / ``compute_contabo`` modules (no supported
OpenTofu provider) and invoked by Tofu's local-exec; do not run by hand.
Replaces ``scripts/apply-provider-firewall.sh``.

Inputs (environment): ``SPEC_PATH``, ``SERVER_ID``, ``RULES_JSON``,
``API_USERNAME``, ``API_PASSWORD``, ``API_TOKEN``.
"""
from __future__ import annotations

import base64
import os
import sys
import urllib.error
import urllib.request

import yaml


def _spec(path: str) -> dict:
    with open(path, encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def run(env: dict) -> int:
    spec_path = env.get("SPEC_PATH", "")
    server_id = env.get("SERVER_ID", "")
    if not spec_path or not server_id:
        print("sealedstack: apply-provider-firewall needs SPEC_PATH and SERVER_ID", file=sys.stderr)
        return 1
    spec = _spec(spec_path)
    firewall = spec.get("firewall") or {}
    base_url = str(spec.get("base_url") or "")
    method = str(firewall.get("method") or "post").upper()
    path = str(firewall.get("path") or "").replace("{server_id}", server_id)
    body = str(firewall.get("body") or "")
    auth = str((spec.get("auth") or {}).get("type") or "basic")

    headers = {"Content-Type": "application/json"}
    if auth == "basic":
        user = env.get("API_USERNAME", "")
        password = env.get("API_PASSWORD", "")
        if not user or not password:
            print("sealedstack: API_USERNAME/API_PASSWORD must be set for basic auth", file=sys.stderr)
            return 1
        token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    elif auth == "bearer":
        if not env.get("API_TOKEN"):
            print("sealedstack: API_TOKEN must be set for bearer auth", file=sys.stderr)
            return 1
        headers["Authorization"] = f"Bearer {env['API_TOKEN']}"
    else:
        print(f"sealedstack: unknown auth type: {auth}", file=sys.stderr)
        return 1

    payload = body.replace("{{rules}}", env.get("RULES_JSON", "")).encode("utf-8")
    print(f"apply-provider-firewall: {method} {base_url}{path}")
    request = urllib.request.Request(f"{base_url}{path}", data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(response.read().decode("utf-8", "replace"))
        return 0
    except urllib.error.HTTPError as exc:
        print(f"sealedstack: firewall request failed (HTTP {exc.code}): {exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"sealedstack: firewall request failed: {exc.reason}", file=sys.stderr)
        return 1


def main(argv=None) -> int:
    return run(dict(os.environ))


if __name__ == "__main__":
    raise SystemExit(main())
