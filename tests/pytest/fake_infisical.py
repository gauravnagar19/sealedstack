"""A local fake Infisical API for tests: stdlib ``http.server`` on 127.0.0.1.

Strict on purpose, so a wrong URL, header or body fails here instead of on a live
account (the lesson of the S3 bucket bug, DR-1):

- ``POST /api/v1/auth/universal-auth/login`` takes a *form-urlencoded* ``clientId`` and
  ``clientSecret`` and answers ``{"accessToken": ...}`` for a known identity, else 401;
- ``GET /api/v4/secrets`` needs ``projectId`` and ``environment`` (400 otherwise), a
  ``Authorization: Bearer`` token that is either static (Token Auth) or was issued by a
  login (else 401), and an existing project / environment / folder (else 404);
- ``script`` queues canned responses served first (rate limits, outages, 403);
- every request is recorded (method, path, query, form, lower-cased headers).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOGIN = "/api/v1/auth/universal-auth/login"
SECRETS = "/api/v4/secrets"


def error(status, message, kind="Error"):
    return {"reqId": "req-1", "statusCode": status, "message": message, "error": kind}


class FakeInfisical:
    def __init__(self, projects=None, identities=None, tokens=None, hidden=()):
        # projects: {project id: {environment: {folder: {secret name: value}}}}
        self.projects = dict(projects or {})
        self.identities = dict(identities or {})  # client id -> client secret
        self.static_tokens = set(tokens or ())  # Token Auth access tokens
        self.issued: set = set()
        self.hidden = set(hidden)  # names whose value the identity may not see
        self.script: list = []  # (status, headers, body) served before normal handling
        self.requests: list = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        # poll_interval: shutdown() waits for the next poll, 0.5s by default, per fixture.
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the test output quiet
                pass

            def _send(self, status, body, headers=None):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def _record(self, method, form=None):
                parts = urlsplit(self.path)
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append({"method": method, "path": parts.path, "query": parse_qs(parts.query),
                                      "form": form, "headers": headers})
                return parts, headers

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode()
                form = parse_qs(raw)
                parts, headers = self._record("POST", form)
                if fake.script:
                    status, extra, body = fake.script.pop(0)
                    return self._send(status, body, extra)
                if parts.path != LOGIN:
                    return self._send(404, error(404, "Route not found", "NotFound"))
                if headers.get("content-type", "") != "application/x-www-form-urlencoded":
                    return self._send(400, error(400, "Expected a form body", "BadRequest"))
                client_id = (form.get("clientId") or [""])[0]
                client_secret = (form.get("clientSecret") or [""])[0]
                if not client_id or fake.identities.get(client_id) != client_secret:
                    return self._send(401, error(401, "Invalid credentials", "UnauthorizedError"))
                token = f"access-{len(fake.issued) + 1}"
                fake.issued.add(token)
                return self._send(200, {"accessToken": token, "expiresIn": 7200, "accessTokenMaxTTL": 7200,
                                        "tokenType": "Bearer"})

            def do_GET(self):
                parts, headers = self._record("GET")
                if fake.script:
                    status, extra, body = fake.script.pop(0)
                    return self._send(status, body, extra)
                if parts.path != SECRETS:
                    return self._send(404, error(404, "Route not found", "NotFound"))
                auth = headers.get("authorization", "")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                if token not in fake.static_tokens and token not in fake.issued:
                    return self._send(401, error(401, "Token missing or invalid", "UnauthorizedError"))
                query = parse_qs(parts.query)
                project = (query.get("projectId") or [""])[0]
                environment = (query.get("environment") or [""])[0]
                folder = (query.get("secretPath") or ["/"])[0]
                if not project or not environment:
                    return self._send(400, error(400, "projectId and environment are required", "BadRequest"))
                found = ((fake.projects.get(project) or {}).get(environment) or {}).get(folder)
                if found is None:
                    return self._send(404, error(404, f"Folder with path '{folder}' not found", "NotFound"))
                secrets = [
                    {"id": name, "secretKey": name, "secretValue": "" if name in fake.hidden else value,
                     "secretValueHidden": name in fake.hidden, "type": "shared", "version": 1}
                    for name, value in found.items()
                ]
                return self._send(200, {"secrets": secrets, "imports": []})

        return Handler
