"""Dokploy API client (D-047).

A thin wrapper over :class:`sealedstack.http.HttpClient` so the post-apply phase
can read composes, update their env and trigger/poll deploys. The API key is sent
as the ``x-api-key`` header and never logged.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from .http import HttpClient, UrllibClient


class DokployError(RuntimeError):
    """A Dokploy API call failed."""


class DokployClient:
    def __init__(self, endpoint: str, api_key: str, *, http: Optional[HttpClient] = None, timeout: float = 30.0):
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.http = http or UrllibClient()
        self.timeout = timeout

    def _call(self, method: str, path: str, json_body: Any = None) -> Any:
        url = f"{self.endpoint}{path}"
        response = self.http.request(
            method,
            url,
            headers={"x-api-key": self.api_key, "Accept": "application/json"},
            json_body=json_body,
            timeout=self.timeout,
        )
        if response.status >= 400:
            raise DokployError(f"{method} {path} failed (HTTP {response.status})")
        return response.json()

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self._call("POST", path, json_body=body)

    # -- composes -----------------------------------------------------------
    def compose(self, compose_id: str) -> dict:
        return self.get(f"/api/compose.one?composeId={compose_id}") or {}

    def compose_env(self, compose_id: str) -> str:
        return str(self.compose(compose_id).get("env") or "")

    def compose_update(self, compose_id: str, env: str) -> Any:
        return self.post("/api/compose.update", {"composeId": compose_id, "env": env})

    def compose_deploy(self, compose_id: str) -> Any:
        return self.post("/api/compose.deploy", {"composeId": compose_id})

    def deployments(self, compose_id: str) -> list:
        data = self.get(f"/api/deployment.allByCompose?composeId={compose_id}")
        return data or []


def control_host(config: config_mod.Config) -> str:
    """The control server as a MagicDNS name (FQDN when mesh.tailnet is set)."""
    servers = config.servers.get("servers") or {}
    name = next((n for n, s in servers.items() if (s or {}).get("role") == "control"), "")
    suffix = str((config.stack.get("mesh") or {}).get("tailnet") or "")
    return f"{name}.{suffix}" if suffix else name


def from_config(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    source: Optional[secrets_mod.SecretSource] = None,
    *,
    http: Optional[HttpClient] = None,
) -> DokployClient:
    env = dict(env or {})
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    key = config.secret_key("DOKPLOY_API_KEY")
    result = source.get(key)
    if not result.found:
        raise DokployError(f"DOKPLOY_API_KEY not found in the '{source.kind}' source")
    return DokployClient(f"http://{control_host(config)}:3000", result.value, http=http)
