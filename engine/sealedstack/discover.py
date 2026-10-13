"""Facts the Setup PR can fill in for the user once the credentials exist.

Setup asks for nothing beyond the six flags (UX §4 step 3). The Cloudflare account
id, the Zero Trust team name, the MagicDNS suffix and the user's current tailnet
policy are all discoverable with the credentials the user adds next, so ``check``
proposes them as commits on the Setup branch. Read-only API calls.
"""
from __future__ import annotations

import base64
import re

from .http import HttpClient

CF_API = "https://api.cloudflare.com/client/v4"
TS_API = "https://api.tailscale.com/api/v2"


def _json(response) -> dict:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def tailscale_token(http: HttpClient, client_id: str, client_secret: str) -> str:
    """An access token for the OAuth client (client-credentials flow), or ""."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    response = http.request(
        "POST", f"{TS_API}/oauth/token",
        headers={"Authorization": f"Basic {basic}"}, form_body={"grant_type": "client_credentials"},
    )
    return str(_json(response).get("access_token") or "") if response.status == 200 else ""


def tailnet_suffix(http: HttpClient, token: str) -> str:
    """The MagicDNS suffix (``tail1234.ts.net``), read from any device's name."""
    response = http.request("GET", f"{TS_API}/tailnet/-/devices", headers={"Authorization": f"Bearer {token}"})
    if response.status != 200:
        return ""
    for device in _json(response).get("devices") or []:
        name = str(device.get("name") or "")
        if "." in name:
            suffix = name.split(".", 1)[1].strip(".")
            if re.fullmatch(r"[a-z0-9-]+\.ts\.net", suffix) or "." in suffix:
                return suffix
    return ""


def tailscale_policy(http: HttpClient, token: str) -> str:
    """The current tailnet policy as HuJSON text, or "" when it cannot be read."""
    response = http.request(
        "GET", f"{TS_API}/tailnet/-/acl",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/hujson"},
    )
    return response.text if response.status == 200 else ""


def cloudflare_account(http: HttpClient, cf_token: str) -> str:
    """The account id, when the token can see exactly one account (else "")."""
    response = http.request("GET", f"{CF_API}/accounts?per_page=5", headers={"Authorization": f"Bearer {cf_token}"})
    if response.status != 200:
        return ""
    accounts = _json(response).get("result") or []
    return str(accounts[0].get("id") or "") if len(accounts) == 1 else ""


def cloudflare_team(http: HttpClient, cf_token: str, account_id: str) -> str:
    """The Zero Trust team name (the first label of ``<team>.cloudflareaccess.com``)."""
    response = http.request(
        "GET", f"{CF_API}/accounts/{account_id}/access/organizations",
        headers={"Authorization": f"Bearer {cf_token}"},
    )
    if response.status != 200:
        return ""
    domain = str((_json(response).get("result") or {}).get("auth_domain") or "")
    return domain.split(".")[0] if domain.endswith(".cloudflareaccess.com") else ""


# --------------------------------------------------------------------------- #
# editing stack.yaml in place (comments and the user's edits are kept)
# --------------------------------------------------------------------------- #
PLACEHOLDER = "REPLACE_ME"


def fill_stack(text: str, *, account_id: str = "", team_name: str = "", tailnet: str = "") -> tuple[str, list]:
    """Fill the placeholders setup left; returns ``(new_text, [what changed])``.

    ``account_id`` and ``tailnet`` are only set while they are still unset; the team
    name is corrected whenever Cloudflare says it differs (it must match exactly).
    """
    changed: list[str] = []

    if account_id:
        new = re.sub(
            rf"^(\s*account_id:\s*)(?:{PLACEHOLDER}|''|\"\")?(\s*(?:#.*)?)$", rf"\g<1>{account_id}\g<2>", text, count=1,
            flags=re.MULTILINE,
        )
        if new != text:
            text, changed = new, changed + ["cloudflare.account_id"]

    if team_name:
        def team(match):
            return match.group(0) if match.group(2) == team_name else f"{match.group(1)}{team_name}{match.group(3)}"

        new = re.sub(r"^(\s*team_name:\s*)([A-Za-z0-9-]+)(\s*(?:#.*)?)$", team, text, count=1, flags=re.MULTILINE)
        if new != text:
            text, changed = new, changed + ["cloudflare.team_name"]

    if tailnet:
        new = re.sub(
            r"^(\s*tailnet:\s*)(?:''|\"\")?(\s*(?:#.*)?)$", rf"\g<1>{tailnet}\g<2>", text, count=1, flags=re.MULTILINE
        )
        if new != text:
            text, changed = new, changed + ["mesh.tailnet"]
    return text, changed
