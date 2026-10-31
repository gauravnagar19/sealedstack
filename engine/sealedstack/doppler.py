"""Doppler as a read-only secret source (D-066).

``slots.secrets: doppler`` reads sealedstack's secrets from one Doppler *config*
over Doppler's REST API; nothing is ever written back (D-038). No ``doppler`` CLI is
needed (the engine image does not ship one), only HTTPS.

Authentication is a **service token** (``dp.st.…``). It is read-only by default and
scoped to ONE project and config by Doppler itself, so the only secret CI holds is a
token that can read exactly the config you created for sealedstack. Personal, CLI and
service-account tokens are refused on purpose: they are not scoped to one config.

* ``DOPPLER_TOKEN`` reads the infra secrets (the same role as Bitwarden's ``infra``
  project).
* ``DOPPLER_TOKEN_APPS`` (optional) reads the app secrets from a second config. When it
  is not set, app secrets are read from the same config as the infra secrets.

One call (``GET /v3/configs/config/secrets/download?format=json``) returns the whole
config, so a run makes a single request however many secrets it looks up. Doppler's
secret names are upper-case letters, digits and underscores (no dots), so a key such as
``shop.CLERK_SECRET_KEY`` is looked up as ``SHOP_CLERK_SECRET_KEY`` (see ``candidates``).
The token is never printed, logged or put in an error message.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Mapping, Optional

from .http import HttpClient, Response, UrllibClient, one_line, retry_wait
from .secrets import SecretResult, SecretSource

API = "https://api.doppler.com"
DOWNLOAD_PATH = "/v3/configs/config/secrets/download?format=json"
TOKEN_PREFIX = "dp.st."  # a Doppler service token
INFRA_TOKEN = "DOPPLER_TOKEN"
APPS_TOKEN = "DOPPLER_TOKEN_APPS"
DOCS = "https://docs.doppler.com/docs/service-tokens"

RETRY_AFTER_CAP = 30.0
_NAME_OK = re.compile(r"[^A-Z0-9_]")


def doppler_name(key: str) -> str:
    """The Doppler secret name for a sealedstack key.

    Doppler's default naming mode allows only ``A-Z``, ``0-9`` and ``_`` and forbids a
    leading digit, so everything else becomes ``_`` and the name is upper-cased:
    ``shop.CLERK_SECRET_KEY`` -> ``SHOP_CLERK_SECRET_KEY``.
    """
    name = _NAME_OK.sub("_", str(key).upper())
    return f"_{name}" if name[:1].isdigit() else name


def candidates(key: str) -> list[str]:
    """Names to try, in order: the key as written (workspaces in Doppler's permissive
    naming mode keep dots), then the normalised name."""
    names = [str(key)]
    normalised = doppler_name(key)
    if normalised not in names:
        names.append(normalised)
    return names


_one_line = one_line


def _messages(response: Response) -> str:
    """Doppler's own error text (``{"messages": [...], "success": false}``), never a secret."""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - not JSON
        return ""
    if isinstance(data, dict):
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            return _one_line(messages[0])
    return ""


def _retry_after(response: Response, attempt: int) -> float:
    return retry_wait(response, attempt, RETRY_AFTER_CAP)


class DopplerSource(SecretSource):
    """Read-only access to one Doppler config through a service token."""

    kind = "doppler"

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        *,
        apps: bool = False,
        http: Optional[HttpClient] = None,
        api: str = API,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        self.env = dict(env or {})
        self.apps = apps
        self.http = http or UrllibClient()
        self.api = api.rstrip("/")
        self._sleep = sleep
        self.attempts = max(1, attempts)
        self._cache: Optional[dict[str, str]] = None
        self._error = ""

    # -- the token ---------------------------------------------------------
    @property
    def token_vars(self) -> tuple[str, ...]:
        """The environment variables read for the token, most specific first."""
        return (APPS_TOKEN, INFRA_TOKEN) if self.apps else (INFRA_TOKEN,)

    def _token(self) -> tuple[str, str]:
        """``(variable name, token)``; the token is empty when none is set."""
        for var in self.token_vars:
            value = str(self.env.get(var) or "").strip()
            if value:
                return var, value
        return self.token_vars[0], ""

    # -- loading -----------------------------------------------------------
    def _fail(self, message: str) -> RuntimeError:
        self._error = message
        return RuntimeError(message)

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if self._error:  # one failing call per run, not one per secret
            raise RuntimeError(self._error)

        var, token = self._token()
        if not token:
            wanted = " or ".join(self.token_vars)
            raise self._fail(f"{wanted} is not set (create a Doppler service token: {DOCS})")
        if not token.startswith(TOKEN_PREFIX):
            raise self._fail(
                f"{var} is not a Doppler service token (it must start with '{TOKEN_PREFIX}'). Personal, CLI and "
                f"service-account tokens are not supported because they are not scoped to one config ({DOCS})"
            )

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        url = f"{self.api}{DOWNLOAD_PATH}"
        response: Optional[Response] = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.http.request("GET", url, headers=headers)
            except Exception as exc:  # noqa: BLE001 - transport failure
                # str(exc) cannot hold the token: it is only ever in the header.
                raise self._fail(f"could not reach Doppler ({_one_line(exc)})") from exc
            retryable = response.status == 429 or response.status >= 500
            if retryable and attempt < self.attempts:
                self._sleep(_retry_after(response, attempt))
                continue
            break
        assert response is not None
        problem = self._explain(response, var)
        if problem:
            raise self._fail(problem)

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._fail("Doppler returned a body that is not JSON") from exc
        if not isinstance(data, dict):
            raise self._fail("Doppler returned an unexpected body (not a JSON object of secrets)")
        self._cache = {str(k): "" if v is None else str(v) for k, v in data.items()}
        return self._cache

    @staticmethod
    def _explain(response: Response, var: str) -> str:
        """An error sentence for a non-200 response, or ``""`` for success."""
        status = response.status
        if status == 200:
            return ""
        said = _messages(response)
        suffix = f": {said}" if said else ""
        if status == 401:
            return f"Doppler rejected {var} (HTTP 401): it is invalid, revoked or expired{suffix}"
        if status == 403:
            return f"{var} is not allowed to read secrets (HTTP 403): use a service token with read access{suffix}"
        if status == 404:
            return f"Doppler found no config for {var} (HTTP 404): the token's project or config was deleted{suffix}"
        if status == 429:
            return f"Doppler rate limit reached (HTTP 429); wait a minute and run again{suffix}"
        return f"Doppler answered HTTP {status}{suffix}"

    # -- the interface -----------------------------------------------------
    def get(self, name: str) -> SecretResult:
        # An exported variable always wins, like every other source, so an operator can
        # override one value locally (and `check` finds DOPPLER_TOKEN itself this way).
        if self.env.get(name):
            return SecretResult(name, "found", self.env[name])
        try:
            secrets = self._load()
        except RuntimeError as exc:
            return SecretResult(name, "error", detail=str(exc))
        tried = candidates(name)
        for candidate in tried:
            if candidate in secrets:
                value = secrets[candidate]
                if value == "":
                    return SecretResult(name, "missing", detail=f"{candidate} is set in Doppler but empty")
                return SecretResult(name, "found", value)
        shown = " or ".join(tried)
        return SecretResult(name, "missing", detail=f"{shown} not found in the Doppler config of the service token")
