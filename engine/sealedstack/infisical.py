"""Infisical as a read-only secret source (D-067).

``slots.secrets: infisical`` reads sealedstack's secrets from one Infisical *project
environment* (and folder) over Infisical's REST API; nothing is ever written back
(D-038). No ``infisical`` CLI is needed (the engine image does not ship one), only HTTPS.
Infisical Cloud (US and EU) and self-hosted instances work: set ``site_url``.

Authentication is a **machine identity** that has read access to that one environment:

* ``INFISICAL_CLIENT_ID`` + ``INFISICAL_CLIENT_SECRET`` (Universal Auth, the recommended
  method): exchanged for a short-lived access token at the start of every run, so the
  secrets held in GitHub never expire into a stale token.
* ``INFISICAL_TOKEN`` (Token Auth): an access token used as is. It expires after its TTL
  (30 days by default) and then must be replaced, so Universal Auth is easier to keep
  running. If both are set the token wins.

Which secrets are read is NON-secret config, ``providers.secrets_infisical`` in
``config/stack.yaml`` (or ``config/providers/secrets_infisical.yaml``):

* ``project_id`` (required): the project's ID, from its settings page;
* ``environment``: the environment slug (default ``prod``);
* ``secret_path``: the folder holding the infra secrets (default ``/``);
* ``apps_secret_path``: the folder holding the app secrets (default: ``secret_path``);
* ``site_url``: ``https://app.infisical.com`` (default), ``https://eu.infisical.com``
  or your own host. Must be https.

One call (``GET /api/v4/secrets``) returns the whole folder, so a run makes one request
(plus one login) however many secrets it looks up. The access token, client secret and
secret values are never printed, logged or put in an error message.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional
from urllib.parse import urlencode, urlsplit

import yaml

from .http import HttpClient, Response, UrllibClient, one_line, retry_wait
from .secrets import SecretResult, SecretSource

API = "https://app.infisical.com"
SECRETS_PATH = "/api/v4/secrets"
LOGIN_PATH = "/api/v1/auth/universal-auth/login"
TOKEN_VAR = "INFISICAL_TOKEN"
CLIENT_ID_VAR = "INFISICAL_CLIENT_ID"
CLIENT_SECRET_VAR = "INFISICAL_CLIENT_SECRET"
CREDENTIAL_VARS = (TOKEN_VAR, CLIENT_ID_VAR, CLIENT_SECRET_VAR)
DOCS = "https://infisical.com/docs/documentation/platform/identities/universal-auth"
CONFIG_KEY = "secrets_infisical"

DEFAULT_ENVIRONMENT = "prod"
RETRY_AFTER_CAP = 30.0
_NAME_OK = re.compile(r"[^A-Z0-9_]")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def infisical_name(key: str) -> str:
    """The upper-case, underscore-only form of a sealedstack key (the form that works
    wherever a secret is created): ``shop.CLERK_SECRET_KEY`` -> ``SHOP_CLERK_SECRET_KEY``."""
    name = _NAME_OK.sub("_", str(key).upper())
    return f"_{name}" if name[:1].isdigit() else name


def candidates(key: str) -> list[str]:
    """Names to try, in order: the key as written, then the normalised name."""
    names = [str(key)]
    normalised = infisical_name(key)
    if normalised not in names:
        names.append(normalised)
    return names


def normalise_path(path: object) -> str:
    """``apps`` / ``/apps/`` -> ``/apps``; empty -> ``/``."""
    text = "/" + str(path or "").strip().strip("/")
    return text if text != "/" else "/"


@dataclass(frozen=True)
class Settings:
    """The non-secret part of the source: where in Infisical the secrets live."""

    project_id: str = ""
    environment: str = DEFAULT_ENVIRONMENT
    secret_path: str = "/"
    apps_secret_path: str = ""
    site_url: str = API

    def path_for(self, apps: bool) -> str:
        return normalise_path(self.apps_secret_path if apps and self.apps_secret_path else self.secret_path)

    def problem(self) -> str:
        """What is wrong with the settings, or ``""``."""
        if not self.project_id:
            return (
                f"providers.{CONFIG_KEY}.project_id is not set in config/stack.yaml "
                "(the project's ID, shown in its settings in Infisical)"
            )
        parts = urlsplit(self.site_url)
        host = parts.hostname or ""
        if not host or not (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK)):
            return f"providers.{CONFIG_KEY}.site_url must be an https:// address (got '{one_line(self.site_url, 80)}')"
        return ""

    def describe(self, apps: bool = False) -> str:
        return f"Infisical project {self.project_id or '(not set)'}, environment {self.environment}, path {self.path_for(apps)}"


def load_settings(stack: Mapping, config_dir: Optional[Path] = None) -> Settings:
    """Settings from ``config/providers/secrets_infisical.yaml``, else ``stack.yaml``."""
    from_stack = (stack.get("providers") or {}).get(CONFIG_KEY) or {}
    from_file: dict = {}
    if config_dir is not None:
        file = Path(config_dir) / "providers" / f"{CONFIG_KEY}.yaml"
        if file.exists():
            try:
                loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
                from_file = loaded if isinstance(loaded, dict) else {}
            except yaml.YAMLError:
                from_file = {}

    def pick(key: str, default: str) -> str:
        for source in (from_file, from_stack):
            value = source.get(key)
            if value not in (None, ""):
                return str(value).strip()
        return default

    return Settings(
        project_id=pick("project_id", ""),
        environment=pick("environment", DEFAULT_ENVIRONMENT),
        secret_path=pick("secret_path", "/"),
        apps_secret_path=pick("apps_secret_path", ""),
        site_url=pick("site_url", API).rstrip("/"),
    )


def _message(response: Response) -> str:
    """Infisical's own error text (``{"message": ..., "error": ...}``), never a secret."""
    try:
        data = response.json()
    except Exception:  # noqa: BLE001 - not JSON
        return ""
    if isinstance(data, dict):
        text = data.get("message") or data.get("error")
        if text:
            return one_line(text)
    return ""


class InfisicalSource(SecretSource):
    """Read-only access to one Infisical folder through a machine identity."""

    kind = "infisical"

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        settings: Optional[Settings] = None,
        *,
        apps: bool = False,
        http: Optional[HttpClient] = None,
        sleep: Callable[[float], None] = time.sleep,
        attempts: int = 3,
    ) -> None:
        self.env = dict(env or {})
        self.settings = settings or Settings()
        self.apps = apps
        self.http = http or UrllibClient()
        self.api = self.settings.site_url.rstrip("/")
        self._sleep = sleep
        self.attempts = max(1, attempts)
        self._cache: Optional[dict[str, str]] = None
        self._hidden: set[str] = set()
        self._error = ""

    # -- the credentials ---------------------------------------------------
    @property
    def token_vars(self) -> tuple[str, ...]:
        """Every environment variable that can carry the source's credentials."""
        return CREDENTIAL_VARS

    def _value(self, var: str) -> str:
        return str(self.env.get(var) or "").strip()

    def credentials_problem(self) -> str:
        """What is missing from the environment, or ``""`` when a login is possible."""
        if self._value(TOKEN_VAR):
            return ""
        client_id, client_secret = self._value(CLIENT_ID_VAR), self._value(CLIENT_SECRET_VAR)
        if client_id and client_secret:
            return ""
        if client_id or client_secret:
            missing = CLIENT_SECRET_VAR if client_id else CLIENT_ID_VAR
            return f"{missing} is not set (Universal Auth needs both {CLIENT_ID_VAR} and {CLIENT_SECRET_VAR})"
        return f"{CLIENT_ID_VAR} and {CLIENT_SECRET_VAR} (or {TOKEN_VAR}) are not set (create a machine identity: {DOCS})"

    # -- talking to Infisical ----------------------------------------------
    def _fail(self, message: str) -> RuntimeError:
        self._error = message
        return RuntimeError(message)

    def _call(self, method: str, url: str, **kwargs) -> Response:
        """One request with retries on 429 / 5xx; a transport failure is a clear error."""
        response: Optional[Response] = None
        for attempt in range(1, self.attempts + 1):
            try:
                response = self.http.request(method, url, **kwargs)
            except Exception as exc:  # noqa: BLE001 - transport failure
                # str(exc) cannot hold a credential: they are only ever in a header or form body.
                raise self._fail(f"could not reach Infisical at {self.api} ({one_line(exc)})") from exc
            retryable = response.status == 429 or response.status >= 500
            if retryable and attempt < self.attempts:
                self._sleep(retry_wait(response, attempt, RETRY_AFTER_CAP))
                continue
            break
        assert response is not None
        return response

    def _access_token(self) -> str:
        token = self._value(TOKEN_VAR)
        if token:
            return token
        response = self._call(
            "POST", f"{self.api}{LOGIN_PATH}",
            headers={"Accept": "application/json"},
            form_body={"clientId": self._value(CLIENT_ID_VAR), "clientSecret": self._value(CLIENT_SECRET_VAR)},
        )
        if response.status != 200:
            raise self._fail(self._explain_login(response))
        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._fail("Infisical's login answered with a body that is not JSON") from exc
        access = data.get("accessToken") if isinstance(data, dict) else None
        if not access or not isinstance(access, str):
            raise self._fail("Infisical's login answered without an access token")
        return access

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if self._error:  # one failing call per run, not one per secret
            raise RuntimeError(self._error)

        problem = self.settings.problem() or self.credentials_problem()
        if problem:
            raise self._fail(problem)

        bearer = self._access_token()
        query = urlencode({
            "projectId": self.settings.project_id,
            "environment": self.settings.environment,
            "secretPath": self.settings.path_for(self.apps),
            "recursive": "false",
            "expandSecretReferences": "true",
            "viewSecretValue": "true",
            "includeImports": "false",
        })
        response = self._call(
            "GET", f"{self.api}{SECRETS_PATH}?{query}",
            headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json"},
        )
        if response.status != 200:
            raise self._fail(self._explain(response))

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            raise self._fail("Infisical returned a body that is not JSON") from exc
        listed = data.get("secrets") if isinstance(data, dict) else None
        if not isinstance(listed, list):
            raise self._fail("Infisical returned an unexpected body (no list of secrets)")
        cache: dict[str, str] = {}
        for item in listed:
            if not isinstance(item, dict) or not item.get("secretKey"):
                continue
            key = str(item["secretKey"])
            if item.get("secretValueHidden"):
                self._hidden.add(key)
                continue
            value = item.get("secretValue")
            cache[key] = "" if value is None else str(value)
        self._cache = cache
        return cache

    def _explain_login(self, response: Response) -> str:
        said = _message(response)
        suffix = f": {said}" if said else ""
        status = response.status
        if status in (400, 401):
            return (
                f"Infisical rejected {CLIENT_ID_VAR} / {CLIENT_SECRET_VAR} (HTTP {status}): the client secret is wrong, "
                f"revoked or expired, or this address is not a trusted IP of the identity{suffix}"
            )
        if status == 403:
            return f"Infisical refused the login (HTTP 403): the identity is not allowed to log in from here{suffix}"
        if status == 404:
            return f"Infisical found no Universal Auth login at {self.api} (HTTP 404): check providers.{CONFIG_KEY}.site_url{suffix}"
        if status == 429:
            return f"Infisical rate limit reached (HTTP 429); wait a minute and run again{suffix}"
        return f"Infisical's login answered HTTP {status}{suffix}"

    def _explain(self, response: Response) -> str:
        """An error sentence for a non-200 answer to the secrets call."""
        said = _message(response)
        suffix = f": {said}" if said else ""
        status = response.status
        where = f"environment '{self.settings.environment}', path '{self.settings.path_for(self.apps)}'"
        if status == 401:
            return (
                f"Infisical rejected the access token (HTTP 401): {TOKEN_VAR} is invalid or expired (Token Auth "
                f"tokens expire after their TTL; Universal Auth does not){suffix}"
                if self._value(TOKEN_VAR)
                else f"Infisical rejected the access token it was given at login (HTTP 401){suffix}"
            )
        if status == 403:
            return f"the identity may not read secrets in {where} (HTTP 403): give it a read role on that project{suffix}"
        if status == 404:
            return f"Infisical found no such project, environment or folder (HTTP 404): {where}{suffix}"
        if status in (400, 422):
            return f"Infisical rejected the request (HTTP {status}): check project_id, environment and secret_path{suffix}"
        if status == 429:
            return f"Infisical rate limit reached (HTTP 429); wait a minute and run again{suffix}"
        return f"Infisical answered HTTP {status}{suffix}"

    # -- the interface -----------------------------------------------------
    def get(self, name: str) -> SecretResult:
        # An exported variable always wins, like every other source, so an operator can
        # override one value locally (and `check` finds the credentials themselves this way).
        if self.env.get(name):
            return SecretResult(name, "found", self.env[name])
        try:
            secrets = self._load()
        except RuntimeError as exc:
            return SecretResult(name, "error", detail=str(exc))
        tried = candidates(name)
        for candidate in tried:
            if candidate in self._hidden:
                return SecretResult(
                    name, "error",
                    detail=f"{candidate} exists but its value is hidden from the identity: grant it the 'read value' permission",
                )
            if candidate in secrets:
                value = secrets[candidate]
                if value == "":
                    return SecretResult(name, "missing", detail=f"{candidate} is set in Infisical but empty")
                return SecretResult(name, "found", value)
        shown = " or ".join(tried)
        return SecretResult(name, "missing", detail=f"{shown} not found in Infisical ({self.settings.describe(self.apps)})")
