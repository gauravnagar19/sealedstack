"""An in-memory Dokploy v0.30.8 with its member permission model (no network).

It mirrors what the pinned source does, so tests prove the scoping against the
same rules, not against assumptions:

- an owner key (admin) may use ``user.*`` and every ``compose.*`` call;
- a member may call ``compose.one`` / ``deployment.allByCompose`` only on composes in
  ``accessedServices`` (read), ``compose.update`` only with ``canCreateServices``
  and an accessed compose, ``compose.deploy`` on an accessed compose; ``user.*``
  admin calls are refused; ``project.all`` lists only ``accessedProjects``;
- ``user.createUserWithCredentials`` refuses the owner role and an existing e-mail;
- API keys are created for the signed-in user only (cookie), once.
"""
from __future__ import annotations

import itertools
import json

from sealedstack.http import HttpClient, Response

ADMIN_KEY = "dk_admin_example_key"


class FakeDokploy(HttpClient):
    def __init__(self, composes=None, admin_key=ADMIN_KEY):
        # composes: {compose_id: (project_id, environment_id)}
        self.composes = dict(composes or {})
        self.admin_key = admin_key
        self.users = {}        # user_id -> {email, password, role, perms}
        self.keys = {}         # key -> user_id
        self.sessions = {}     # cookie -> user_id
        self.updated = []      # (user_id or "owner", compose_id, body)
        self.calls = []        # (method, path, body)
        self._ids = itertools.count(1)

    # -- helpers --------------------------------------------------------------
    def _reply(self, status, body=None, cookies=None):
        return Response(status, {}, json.dumps(body if body is not None else {}).encode(), "", set_cookies=cookies or [])

    def _caller(self, headers):
        key = headers.get("x-api-key", "")
        if key == self.admin_key:
            return "owner"
        if key in self.keys:
            return self.keys[key]
        cookie = headers.get("Cookie", "")
        for value, user in self.sessions.items():
            if value in cookie:
                return user
        return None

    def member(self, email):
        return next((uid for uid, u in self.users.items() if u["email"] == email), None)

    # -- the API ----------------------------------------------------------------
    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        headers = headers or {}
        path = url.split(":3000", 1)[1]
        route, _, query = path.partition("?")
        self.calls.append((method, path, json_body))
        caller = self._caller(headers)

        if route == "/api/auth/sign-in/email":
            for uid, user in self.users.items():
                if user["email"] == json_body["email"] and user["password"] == json_body["password"]:
                    cookie = f"better-auth.session_token=sess-{uid}"
                    self.sessions[cookie] = uid
                    return self._reply(200, {"ok": 1}, [cookie + "; Path=/"])
            return self._reply(401)
        if caller is None:
            return self._reply(401, {"message": "Unauthorized"})

        owner = caller == "owner"
        perms = {} if owner else self.users[caller]["perms"]

        if route == "/api/user.session":
            return self._reply(200, {"session": {"activeOrganizationId": "org1"}, "user": {"id": caller}})
        if route == "/api/user.createApiKey":
            if "Cookie" not in headers:
                return self._reply(401)
            assert json_body["metadata"] == {"organizationId": "org1"}
            assert json_body.get("rateLimitEnabled") is False  # a default key is rejected after ~5 calls (live, v0.30.8)
            key = f"dk_member_{next(self._ids)}"
            self.keys[key] = caller
            return self._reply(200, {"id": "k", "key": key, "name": json_body["name"]})
        if route.startswith("/api/user."):
            if not owner:
                return self._reply(401, {"message": "Permission denied"})
            if route == "/api/user.all":
                return self._reply(200, [{"userId": uid, "role": u["role"], "user": {"email": u["email"]}} for uid, u in self.users.items()])
            if route == "/api/user.createUserWithCredentials":
                if json_body["role"] == "owner":
                    return self._reply(403, {"message": "Cannot create a user with the owner role"})
                if len(json_body["password"]) < 8:
                    return self._reply(400)
                if self.member(json_body["email"].lower()):
                    return self._reply(400, {"message": "This email already has an account."})
                uid = f"user{next(self._ids)}"
                self.users[uid] = {"email": json_body["email"].lower(), "password": json_body["password"], "role": json_body["role"],
                                   "perms": {"accessedProjects": [], "accessedEnvironments": [], "accessedServices": [], "canCreateServices": False}}
                return self._reply(200)
            if route == "/api/user.assignPermissions":
                required = {"id", "accessedProjects", "accessedEnvironments", "accessedServices", "accessedGitProviders", "accessedServers",
                            "canCreateProjects", "canCreateServices", "canDeleteProjects", "canDeleteServices", "canAccessToDocker",
                            "canAccessToTraefikFiles", "canAccessToAPI", "canAccessToSSHKeys", "canAccessToGitProviders",
                            "canDeleteEnvironments", "canCreateEnvironments"}
                assert required <= set(json_body), f"missing: {required - set(json_body)}"
                self.users[json_body["id"]]["perms"] = {k: v for k, v in json_body.items() if k != "id"}
                return self._reply(200)

        composes = self.composes
        if route == "/api/project.all":
            projects = sorted({p for p, _ in composes.values()})
            if not owner:
                projects = [p for p in projects if p in perms["accessedProjects"]]
            return self._reply(200, [{"projectId": p} for p in projects])

        if route in ("/api/compose.one", "/api/compose.update", "/api/compose.deploy", "/api/deployment.allByCompose"):
            compose_id = (json_body or {}).get("composeId") or dict(q.split("=") for q in query.split("&") if q).get("composeId")
            if compose_id not in composes:
                return self._reply(404)
            if not owner:
                if compose_id not in perms["accessedServices"]:
                    return self._reply(401, {"message": "You don't have access to this service"})
                if route == "/api/compose.update" and not perms["canCreateServices"]:
                    return self._reply(401, {"message": "Permission denied"})
            if route == "/api/compose.one":
                project_id, env_id = composes[compose_id]
                return self._reply(200, {"composeId": compose_id, "env": "", "environmentId": env_id,
                                         "environment": {"environmentId": env_id, "projectId": project_id}})
            if route == "/api/compose.update":
                self.updated.append((caller, compose_id, json_body))
            return self._reply(200, [] if route == "/api/deployment.allByCompose" else {"success": True})

        return self._reply(404)
