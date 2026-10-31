"""A local fake S3 (path-style) for tests: stdlib ``http.server`` on 127.0.0.1.

It is deliberately strict, so the bug class "the client never addressed the
bucket" (DR-1) cannot slip through mocks again:

- the first path segment is the **bucket**; an unknown bucket is ``404 NoSuchBucket``;
- the SigV4 signature is **recomputed from the raw request** (method, raw path,
  received headers) with its own small implementation, so a mismatch between the
  URL that was sent and the canonical URI that was signed is ``403``;
- ``If-None-Match: *`` on PUT is honoured (``412`` when the key exists);
- every request is recorded for assertions.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

ACCESS_KEY = "AKIATESTFAKES3"
SECRET_KEY = "fake-secret-access-key-for-tests"


def _signature(secret: str, date_stamp: str, region: str, string_to_sign: str) -> str:
    def h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = h(h(h(h(("AWS4" + secret).encode(), date_stamp), region), "s3"), "aws4_request")
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


class FakeS3:
    def __init__(self, buckets=("sealedstack-state",), access_key=ACCESS_KEY, secret_key=SECRET_KEY):
        self.buckets = {name: {} for name in buckets}
        self.access_key, self.secret_key = access_key, secret_key
        self.requests: list = []  # dicts: method, path, headers (lowercased), body, status
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()

    def objects(self, bucket):
        return self.buckets[bucket]

    def _verify(self, method, raw_path, headers, payload) -> str:
        """Return '' when the signature is right, else a reason."""
        auth = headers.get("authorization", "")
        if not auth.startswith("AWS4-HMAC-SHA256 "):
            return "no SigV4 Authorization header"
        parts = dict(p.strip().split("=", 1) for p in auth[len("AWS4-HMAC-SHA256 "):].split(","))
        access_key, date_stamp, region, service, term = parts["Credential"].split("/")
        if access_key != self.access_key:
            return "unknown access key"
        if (service, term) != ("s3", "aws4_request"):
            return "bad credential scope"
        signed = parts["SignedHeaders"].split(";")
        if "host" not in signed or "x-amz-date" not in signed:
            return "host and x-amz-date must be signed"
        if hashlib.sha256(payload).hexdigest() != headers.get("x-amz-content-sha256"):
            return "payload hash mismatch"
        path, _, query = raw_path.partition("?")
        canonical_headers = "".join(f"{name}:{headers.get(name, '').strip()}\n" for name in signed)
        canonical = "\n".join([method, path, query, canonical_headers, ";".join(signed), headers["x-amz-content-sha256"]])
        sts = "\n".join(["AWS4-HMAC-SHA256", headers["x-amz-date"], f"{date_stamp}/{region}/s3/aws4_request",
                         hashlib.sha256(canonical.encode()).hexdigest()])
        if _signature(self.secret_key, date_stamp, region, sts) != parts["Signature"]:
            return "SignatureDoesNotMatch"
        return ""

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body=b""):
                fake.requests[-1]["status"] = status
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append({"method": self.command, "path": self.path, "headers": headers, "body": payload})
                problem = fake._verify(self.command, self.path, headers, payload)
                if problem:
                    return self._reply(403, problem.encode())
                segments = self.path.split("?", 1)[0].lstrip("/").split("/", 1)
                bucket = unquote(segments[0])
                key = unquote(segments[1]) if len(segments) > 1 else ""
                if bucket not in fake.buckets:
                    return self._reply(404, b"NoSuchBucket")
                if not key:
                    return self._reply(400, b"bucket-level requests are not supported by the fake")
                store = fake.buckets[bucket]
                if self.command == "PUT":
                    if headers.get("if-none-match") == "*" and key in store:
                        return self._reply(412, b"PreconditionFailed")
                    store[key] = payload
                    return self._reply(200)
                if self.command == "GET":
                    return self._reply(200, store[key]) if key in store else self._reply(404, b"NoSuchKey")
                if self.command == "DELETE":
                    store.pop(key, None)
                    return self._reply(204)
                return self._reply(405)

            do_PUT = do_GET = do_DELETE = _handle

        return Handler
