"""A local fake Doppler API for tests: stdlib ``http.server`` on 127.0.0.1.

Strict on purpose, so a wrong URL or header fails here instead of on a live account
(the lesson of the S3 bucket bug, DR-1):

- only ``GET /v3/configs/config/secrets/download`` exists, and only with ``format=json``;
- the ``Authorization: Bearer <token>`` header decides which config is returned; an
  unknown token is ``401`` with Doppler's ``{"messages": [...], "success": false}`` body;
- ``script`` queues canned responses served first (rate limits, outages, 403, 404);
- every request is recorded (method, path, query, lower-cased headers) for assertions.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

PATH = "/v3/configs/config/secrets/download"


class FakeDoppler:
    def __init__(self, configs=None):
        # service token -> {secret name: value}
        self.configs = dict(configs or {})
        self.script: list = []  # (status, headers, body) served before normal handling
        self.requests: list = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

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

            def do_GET(self):
                parts = urlsplit(self.path)
                headers = {k.lower(): v for k, v in self.headers.items()}
                query = parse_qs(parts.query)
                fake.requests.append({"method": "GET", "path": parts.path, "query": query, "headers": headers})
                if fake.script:
                    status, extra, body = fake.script.pop(0)
                    return self._send(status, body, extra)
                if parts.path != PATH or query.get("format") != ["json"]:
                    return self._send(404, {"messages": ["Unknown route"], "success": False})
                auth = headers.get("authorization", "")
                token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                if token not in fake.configs:
                    return self._send(401, {"messages": ["Invalid Auth token"], "success": False})
                return self._send(200, fake.configs[token])

        return Handler
