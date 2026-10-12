"""A minimal reference app for sealedstack.

One HTTP service: ``GET /api/health`` answers 200 so the platform health check,
the deploy verification and the rollback logic have something to call.
``DATABASE_URL``, ``REDIS_URL`` and ``EXAMPLE_SECRET`` are optional: the app
starts without them and only reports whether they are configured. It never
prints their values.
"""
from __future__ import annotations

import json
import os
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def snapshot(env=None) -> dict:
    env = os.environ if env is None else env
    return {
        "status": "ok",
        "version": env.get("APP_VERSION") or "dev",
        "database": "configured" if env.get("DATABASE_URL") else "not configured",
        "redis": "configured" if env.get("REDIS_URL") else "not configured",
        "secret": "configured" if env.get("EXAMPLE_SECRET") else "not configured",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "example"
    sys_version = ""

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self._send(200, json.dumps(snapshot()).encode(), "application/json")
        elif path == "/":
            self._send(200, b"<!doctype html><title>example</title><h1>example</h1><p>A sealedstack reference app.</p>\n", "text/html; charset=utf-8")
        else:
            self._send(404, b'{"error": "not found"}', "application/json")

    do_HEAD = do_GET  # noqa: N815

    def log_message(self, fmt, *args) -> None:  # request lines only; never headers or bodies
        print(f"{self.address_string()} {fmt % args}", flush=True)


def make_server(port: int, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)


def main() -> None:
    port = int(os.environ.get("PORT") or 3000)
    server = make_server(port)
    # `docker stop` sends SIGTERM; shut down cleanly instead of being killed.
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    print(f"listening on :{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
