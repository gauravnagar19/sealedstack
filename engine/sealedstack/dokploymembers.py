"""A scoped Dokploy member per app, and its API key (D-063, FR-1).

App repos used to receive the Dokploy **admin** API key, which controls every app
on the panel. This module gives each app its own Dokploy *member* instead, allowed
to see and deploy only that app's composes, and returns that member's key for the
app repo's release job.

Endpoints, read from Dokploy v0.30.8 (``apps/dokploy/server/api/routers/user.ts``,
``packages/server/src/services/permission.ts``, ``packages/server/src/lib/auth.ts``,
``openapi.json``):

1. ``GET  /api/user.all`` (admin key): the organisation's members (``userId``, ``user.email``).
2. ``POST /api/user.createUserWithCredentials`` {email, password, role: "member"}
   (admin key; self-hosted only; ``owner`` is refused). No e-mail is sent.
3. ``POST /api/user.assignPermissions`` (admin key) with ``id`` = the member's
   ``userId`` and ``accessedProjects`` / ``accessedEnvironments`` / ``accessedServices``
   (the app's projects, environments and compose ids) plus the ``can*`` flags.
   Every ``compose.*`` call a member makes checks the compose id against
   ``accessedServices``, so the member cannot read or deploy another app.
4. ``POST /api/auth/sign-in/email`` as the member, ``GET /api/user.session``,
   ``POST /api/user.createApiKey`` (cookie): API keys are created for the signed-in
   user only, which is why the member signs in once. The key is shown once, so it
   is stored in the encrypted state before anything else can fail.

``canCreateServices`` has to be true: ``compose.update`` asks for the ``service:create``
permission, which the base ``member`` role only has through that legacy flag (it
still only applies to the member's own projects). What a member **can** still do is
change its own composes, which is why a compose file or command can still reach the
host. See ``docs/limitations.md``; the compose lint (``composelint``) is the second layer.
"""
from __future__ import annotations

import re
import secrets as pysecrets
import sys
from typing import Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import generated as generated_mod
from .dokploybootstrap import _cookie_header, _data
from .http import HttpClient, UrllibClient

MEMBER_DOMAIN = "dokploy.invalid"  # RFC 2606: never resolves, never receives mail
MEMBER_KEY_NAME = "sealedstack-release"

PERMISSION_FLAGS = {
    "canCreateProjects": False,
    "canCreateServices": True,  # required by compose.update; limited to the member's own projects
    "canDeleteProjects": False,
    "canDeleteServices": False,
    "canAccessToDocker": False,
    "canAccessToTraefikFiles": False,
    "canAccessToAPI": False,
    "canAccessToSSHKeys": False,
    "canAccessToGitProviders": False,
    "canDeleteEnvironments": False,
    "canCreateEnvironments": False,
}


class MemberError(config_mod.ConfigError):
    pass


def member_email(app: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", app.lower()).strip("-") or "app"
    return f"deploy-{slug}@{MEMBER_DOMAIN}"


def password_key(app: str) -> str:
    return f"dokploy_member_{app}_password"


def key_key(app: str) -> str:
    return f"dokploy_member_{app}_key"


class Members:
    """The slice of Dokploy's API this module needs."""

    def __init__(self, base_url: str, admin_key: str, *, http: Optional[HttpClient] = None, out=None):
        self.base = base_url.rstrip("/")
        self.admin_key = admin_key
        self.http = http or UrllibClient()
        self.out = out if out is not None else sys.stderr

    # -- plumbing -------------------------------------------------------------
    def _headers(self, *, key: str = "", cookie: str = "") -> dict:
        headers = {"Accept": "application/json", "Content-Type": "application/json", "Origin": self.base}
        if key:
            headers["x-api-key"] = key
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def _admin(self, method: str, path: str, body=None):
        response = self.http.request(method, f"{self.base}{path}", headers=self._headers(key=self.admin_key), json_body=body)
        if response.status >= 400:
            raise MemberError(f"Dokploy {method} {path} failed (HTTP {response.status})")
        return _data(response)

    def status_with(self, key: str, path: str) -> int:
        try:
            return self.http.request("GET", f"{self.base}{path}", headers=self._headers(key=key)).status
        except Exception:  # noqa: BLE001
            return 0

    # -- steps ----------------------------------------------------------------
    def find_member(self, email: str) -> Optional[str]:
        """The member's ``userId``, or None."""
        for row in self._admin("GET", "/api/user.all") or []:
            if str(((row or {}).get("user") or {}).get("email") or "").lower() == email.lower():
                return str(row.get("userId") or "")
        return None

    def create_member(self, email: str, password: str) -> None:
        self._admin("POST", "/api/user.createUserWithCredentials", {"email": email, "password": password, "role": "member"})

    def compose_scope(self, compose_ids) -> "tuple[list, list]":
        """``(project ids, environment ids)`` of these composes."""
        projects, environments = [], []
        for compose_id in compose_ids:
            compose = self._admin("GET", f"/api/compose.one?composeId={compose_id}") or {}
            environment = compose.get("environment") or {}
            env_id = str(compose.get("environmentId") or environment.get("environmentId") or "")
            project_id = str(environment.get("projectId") or (environment.get("project") or {}).get("projectId") or "")
            if env_id and env_id not in environments:
                environments.append(env_id)
            if project_id and project_id not in projects:
                projects.append(project_id)
        return projects, environments

    def assign(self, user_id: str, compose_ids) -> None:
        projects, environments = self.compose_scope(compose_ids)
        body = {
            "id": user_id,
            "accessedProjects": projects,
            "accessedEnvironments": environments,
            "accessedServices": list(compose_ids),
            "accessedGitProviders": [],
            "accessedServers": [],
            **PERMISSION_FLAGS,
        }
        self._admin("POST", "/api/user.assignPermissions", body)

    def create_key(self, email: str, password: str) -> str:
        response = self.http.request(
            "POST", f"{self.base}/api/auth/sign-in/email", headers=self._headers(), json_body={"email": email, "password": password}
        )
        cookie = _cookie_header(response)
        if response.status != 200 or not cookie:
            raise MemberError(f"could not sign in as the Dokploy member {email} (HTTP {response.status})")
        who = self.http.request("GET", f"{self.base}/api/user.session", headers=self._headers(cookie=cookie))
        org = ((_data(who) or {}).get("session") or {}).get("activeOrganizationId") if who.status == 200 else ""
        if not org:
            raise MemberError(f"could not read the Dokploy organisation of {email} (user.session)")
        created = self.http.request(
            "POST", f"{self.base}/api/user.createApiKey", headers=self._headers(cookie=cookie),
            json_body={"name": MEMBER_KEY_NAME, "metadata": {"organizationId": org}},
        )
        data = _data(created) if created.status == 200 else None
        key = str((data or {}).get("key") or "") if isinstance(data, dict) else ""
        if not key:
            raise MemberError(f"creating the API key of {email} failed (HTTP {created.status})")
        return key

    def key_scope_ok(self, key: str, own: list, others: list) -> bool:
        """The key reads its own composes and is refused on every other app's."""
        if not own or any(self.status_with(key, f"/api/compose.one?composeId={c}") != 200 for c in own):
            return False
        return all(self.status_with(key, f"/api/compose.one?composeId={c}") in (401, 403, 404) for c in others)


def ensure(
    config: config_mod.Config,
    apps: Mapping[str, Mapping[str, str]],
    *,
    admin_key: str,
    generated: generated_mod.Generated,
    http: Optional[HttpClient] = None,
    out=None,
    everything: Optional[Mapping[str, Mapping[str, str]]] = None,
    new_password=lambda: pysecrets.token_urlsafe(32),
) -> "dict[str, str]":
    """Make sure each app has a scoped member and a working key; return ``{app: key}``.

    ``apps`` maps an app to its ``{server: compose id}``; ``everything`` is every app's
    (to prove the key is refused on the others). Idempotent: permissions are
    re-asserted on every run (a new server adds a compose id), a stored key that
    still works is reused, and the password and key are stored in the encrypted state
    before anything that could fail after them.
    """
    out = out if out is not None else sys.stderr
    members = Members(f"http://{dokploy_mod.control_host(config)}:3000", admin_key, http=http, out=out)
    result: "dict[str, str]" = {}
    for app, by_server in apps.items():
        own = [str(c) for c in (by_server or {}).values() if c]
        if not own:
            print(f"dokploy: {app}: no compose exists yet, so no scoped key was created (run apply once its server is up)", file=out)
            continue
        others = [str(c) for other, servers in (everything or apps).items() if other != app for c in (servers or {}).values() if c]
        email = member_email(app)
        values = generated.values()
        password = values.get(password_key(app)) or ""
        if not password:
            password = new_password()
            generated.store(**{password_key(app): password})  # before the user exists: a retry can sign in
        user_id = members.find_member(email)
        if not user_id:
            members.create_member(email, password)
            user_id = members.find_member(email)
            if not user_id:
                raise MemberError(f"created the Dokploy member {email} but cannot find it in user.all")
            print(f"dokploy: created the scoped member for {app}", file=out)
        members.assign(user_id, own)
        key = values.get(key_key(app)) or ""
        if key and members.key_scope_ok(key, own, others):
            print(f"dokploy: {app}: the scoped key works and is limited to its own services", file=out)
        else:
            key = members.create_key(email, password)
            generated.store(**{key_key(app): key})  # shown once: keep it before anything else can fail
            if not members.key_scope_ok(key, own, others):
                raise MemberError(
                    f"the new Dokploy key of {app} does not have the expected scope (it must read its own services "
                    "and be refused on other apps'); not publishing it"
                )
            print(f"dokploy: {app}: created a scoped API key and stored it in the encrypted state", file=out)
        result[app] = key
    return result
