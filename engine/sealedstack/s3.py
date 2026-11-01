"""Minimal S3 client with SigV4 signing (D-047 / fixes round 3 C1).

Only what the state lock and the progress record need: a *conditional* ``put``
(``If-None-Match: *``), ``put``, ``get`` and ``delete``. Path-style addressing,
so it works against Cloudflare R2 (region ``auto``): every request goes to
``<endpoint>/<bucket>/<key>`` and the signed canonical URI is ``/<bucket>/<key>``
(DR-1: without the bucket the first key segment is read as the bucket name).
Pure stdlib (no boto3); the network call is injectable for tests.
"""
from __future__ import annotations

import hashlib
import hmac
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Mapping, Optional

from .http import Response

# A sender takes (method, url, headers, body) and returns a Response.
Sender = "Callable[[str, str, Mapping[str, str], bytes], Response]"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


class S3Error(RuntimeError):
    pass


class S3BucketMissing(S3Error):
    """The bucket itself does not exist (R2 answers 404 NoSuchBucket), e.g. before the first ``up``."""


def default_sender(method: str, url: str, headers: Mapping[str, str], body: bytes) -> Response:
    import urllib.request

    request = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return Response(status=int(getattr(response, "status", response.getcode())), headers=dict(response.headers.items()), body=response.read(), url=url)
    except urllib.error.HTTPError as exc:
        return Response(status=exc.code, headers=dict(exc.headers.items()) if exc.headers else {}, body=exc.read(), url=url)


def sign_v4(
    method: str,
    host: str,
    canonical_uri: str,
    payload_hash: str,
    extra: Mapping[str, str],
    *,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime,
    query: str = "",
) -> "tuple[str, dict]":
    """Sign one request with SigV4; return ``(canonical_request, headers_to_send)``.

    ``canonical_uri`` is already percent-encoded (S3 does not double-encode).
    Verified against the published AWS SigV4 S3 examples in the tests.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    signed = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date}
    for name, value in extra.items():
        signed[name.lower()] = value
    signed_headers = ";".join(sorted(signed))
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in sorted(signed))
    canonical_request = "\n".join([method, canonical_uri, query, canonical_headers, signed_headers, payload_hash])
    scope = f"{date_stamp}/{region}/s3/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))])
    signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), date_stamp), region), "s3"), "aws4_request")
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    out = dict(extra)
    out.update(
        {
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }
    )
    return canonical_request, out


class S3Client:
    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        *,
        region: str = "auto",
        sender=None,
        clock=None,
    ):
        if not bucket or "/" in bucket:
            raise ValueError(f"S3Client needs a bucket name (got {bucket!r})")
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket
        self.host = urllib.parse.urlparse(self.endpoint).netloc
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self._send = sender or default_sender
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def path(self, key: str) -> str:
        """The percent-encoded request path (and canonical URI): ``/<bucket>/<key>``."""
        return "/" + urllib.parse.quote(f"{self.bucket}/{key.lstrip('/')}", safe="/")

    def url(self, key: str) -> str:
        return f"{self.endpoint}{self.path(key)}"

    def _request(self, method: str, key: str, *, body: bytes = b"", extra: Optional[Mapping[str, str]] = None) -> Response:
        _, headers = sign_v4(
            method,
            self.host,
            self.path(key),
            _sha256_hex(body),
            dict(extra or {}),
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            now=self._clock(),
        )
        return self._send(method, self.url(key), headers, body)

    def put(self, key: str, body: bytes) -> None:
        """Create or replace ``key``."""
        response = self._request("PUT", key, body=body, extra={"Content-Type": "application/json"})
        if response.status not in (200, 201):
            raise S3Error(f"put {key} failed (HTTP {response.status})")

    def put_if_absent(self, key: str, body: bytes) -> bool:
        """Create ``key`` only if it does not exist. True if created, False if it
        already exists (HTTP 412 Precondition Failed)."""
        response = self._request("PUT", key, body=body, extra={"If-None-Match": "*", "Content-Type": "application/json"})
        if response.status in (200, 201):
            return True
        if response.status == 412:
            return False
        if response.status == 404:
            raise S3BucketMissing(f"bucket '{self.bucket}' does not exist (conditional put {key}: HTTP 404)")
        raise S3Error(f"conditional put {key} failed (HTTP {response.status})")

    def get(self, key: str) -> Optional[bytes]:
        response = self._request("GET", key)
        if response.status == 404:
            return None
        if response.status >= 400:
            raise S3Error(f"get {key} failed (HTTP {response.status})")
        return response.body

    def delete(self, key: str) -> None:
        response = self._request("DELETE", key)
        if response.status not in (200, 202, 204, 404):
            raise S3Error(f"delete {key} failed (HTTP {response.status})")
