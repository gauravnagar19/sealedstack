"""R2 S3 credentials derived from the Cloudflare API token (D-054, implements D-044).

Cloudflare accepts a normal API token as R2 S3 credentials: the access key id is
the token's id and the secret access key is the SHA-256 hex of the token value.
So the user creates no separate "R2 API token"; the Cloudflare token only needs
*Workers R2 Storage: Edit*.

``resolve`` is the single place the engine gets R2 credentials (OpenTofu backend,
edge, state-init, platform, db, the state lock). An explicit
``R2_ACCESS_KEY_ID`` / ``R2_SECRET_ACCESS_KEY`` pair in the secret source still
wins, as an optional override. Nothing derived is ever printed or logged.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from .http import HttpClient, UrllibClient

CF_API = "https://api.cloudflare.com/client/v4"

TOKEN_KEY = "CLOUDFLARE_API_TOKEN"
OVERRIDE_ACCESS = "R2_ACCESS_KEY_ID"
OVERRIDE_SECRET = "R2_SECRET_ACCESS_KEY"

R2_PERMISSION = "Account · Workers R2 Storage: Edit"
TOKEN_URL = "https://dash.cloudflare.com/profile/api-tokens"

# token fingerprint -> token id, so one process verifies a token at most once.
_TOKEN_ID_CACHE: dict[str, str] = {}


class R2Error(config_mod.ConfigError):
    """R2 credentials could not be obtained; the message says what, why and the fix."""


def _fingerprint(token: str) -> str:
    # Not the derived secret: a salted hash used only as an in-memory cache key.
    return hashlib.sha256(b"sealedstack-r2-cache:" + token.encode("utf-8")).hexdigest()


def derive_secret_access_key(token: str) -> str:
    """The R2 secret access key for a Cloudflare API token (SHA-256 hex)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class R2Credentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)
    derived: bool = False

    def __repr__(self) -> str:  # never show the secret, even by accident
        origin = "derived" if self.derived else "override"
        return f"R2Credentials(access_key_id={self.access_key_id!r}, secret_access_key=<hidden>, {origin})"


def _error(what: str, why: str, fix: str) -> R2Error:
    return R2Error(f"{what}\n  Why:  {why}\n  Fix:  {fix}")


def lookup_token_id(
    http: HttpClient,
    token: str,
    account_id: str = "",
) -> str:
    """The Cloudflare token id (``/user/tokens/verify``; account-owned tokens use
    ``/accounts/<id>/tokens/verify``). Raises ``R2Error`` when it can't be verified."""
    key = _fingerprint(token)
    if key in _TOKEN_ID_CACHE:
        return _TOKEN_ID_CACHE[key]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    urls = [f"{CF_API}/user/tokens/verify"]
    if account_id:
        urls.append(f"{CF_API}/accounts/{account_id}/tokens/verify")
    last_status = 0
    for url in urls:
        try:
            response = http.request("GET", url, headers=headers)
        except Exception as exc:  # noqa: BLE001 - transport failure
            raise _error(
                "Could not verify the Cloudflare token (needed to derive the R2 keys).",
                f"The request failed: {exc}",
                "Check that the runner can reach api.cloudflare.com, then re-run.",
            ) from exc
        last_status = response.status
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            data = None
        if response.status == 200 and isinstance(data, dict) and data.get("success"):
            token_id = str((data.get("result") or {}).get("id") or "")
            if token_id:
                _TOKEN_ID_CACHE[key] = token_id
                return token_id
    raise _error(
        "The Cloudflare token could not be verified, so the R2 keys cannot be derived.",
        f"Cloudflare answered HTTP {last_status} for the token verify call.",
        f"Recreate the token at {TOKEN_URL} with the documented permissions "
        f"(including \"{R2_PERMISSION}\") and update it in the secret source.",
    )


def _found(source: secrets_mod.SecretSource, config: config_mod.Config, logical: str) -> Optional[str]:
    result = source.get(config.secret_key(logical))
    return result.value if result.found else None


def resolve(
    source: secrets_mod.SecretSource,
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    http: Optional[HttpClient] = None,
    required: bool = True,
) -> Optional[R2Credentials]:
    """The R2 credentials: an explicit override pair, else derived from the token.

    With ``required=False`` this returns ``None`` when no credentials can be
    formed because nothing is configured (no override and no Cloudflare token).
    A token that is present but cannot be verified still raises ``R2Error``.
    """
    access = _found(source, config, OVERRIDE_ACCESS)
    secret = _found(source, config, OVERRIDE_SECRET)
    if access and secret:
        return R2Credentials(access, secret, derived=False)
    if bool(access) != bool(secret):
        present, absent = (OVERRIDE_ACCESS, OVERRIDE_SECRET) if access else (OVERRIDE_SECRET, OVERRIDE_ACCESS)
        raise _error(
            f"Only one of the optional R2 override keys is set ({config.secret_key(present)}).",
            f"An override needs both {config.secret_key(OVERRIDE_ACCESS)} and {config.secret_key(OVERRIDE_SECRET)}.",
            f"Add {config.secret_key(absent)}, or delete {config.secret_key(present)} "
            "to let sealedstack derive the keys from the Cloudflare token.",
        )

    token_key = config.secret_key(TOKEN_KEY)
    token = _found(source, config, TOKEN_KEY)
    if not token:
        if not required:
            return None
        raise _error(
            "R2 credentials are needed but cannot be derived.",
            f"`cloudflare_api_token` (source key {token_key}) was not found in the "
            f"'{source.kind}' secret source.",
            f"Add {token_key} (with \"{R2_PERMISSION}\") to the secret source.",
        )
    account_id = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
    token_id = lookup_token_id(http or UrllibClient(), token, account_id)
    return R2Credentials(token_id, derive_secret_access_key(token), derived=True)
