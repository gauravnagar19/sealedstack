"""A tiny HTTP client abstraction for the preflight API checks.

The default implementation is ``urllib`` from the standard library, so the
engine image needs no HTTP dependency. Every check takes an ``HttpClient`` so
tests inject a fake and never touch the network.
"""
from __future__ import annotations

import json as _json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional


@dataclass
class Response:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""
    url: str = ""
    set_cookies: list = field(default_factory=list)  # every Set-Cookie header, in order

    def json(self) -> Any:
        if not self.body:
            return None
        return _json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class HttpClient:
    """Interface implemented by the real client and test doubles."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        form_body: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> Response:  # pragma: no cover - interface
        raise NotImplementedError


class UrllibClient(HttpClient):
    """``urllib``-based client. Raises on transport errors; callers catch them."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        form_body: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> Response:
        request_headers = dict(headers or {})
        data: Optional[bytes] = None
        if json_body is not None:
            data = _json.dumps(json_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        req = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(
                    status=int(getattr(resp, "status", resp.getcode())),
                    headers=dict(resp.headers.items()),
                    body=resp.read(),
                    url=url,
                    set_cookies=list(resp.headers.get_all("Set-Cookie") or []),
                )
        except urllib.error.HTTPError as exc:
            # A 4xx/5xx is a normal result the checks inspect (403 = missing
            # permission), not a transport failure.
            return Response(
                status=exc.code,
                headers=dict(exc.headers.items()) if exc.headers else {},
                body=exc.read(),
                url=url,
                set_cookies=list(exc.headers.get_all("Set-Cookie") or []) if exc.headers else [],
            )


@dataclass
class Route:
    """A canned response for ``StubClient``."""

    status: int = 200
    json_body: Any = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    set_cookies: list = field(default_factory=list)


class StubClient(HttpClient):
    """An in-memory client for tests/offline use.

    Routes are matched by a substring of the URL (first match wins); an
    unmatched request returns 404. Every request is recorded for assertions.
    """

    def __init__(self) -> None:
        self.routes: list[tuple[str, Route]] = []
        self.requests: list[dict] = []

    def add(self, url_contains: str, route: Route) -> "StubClient":
        self.routes.append((url_contains, route))
        return self

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        json_body: Any = None,
        form_body: Optional[Mapping[str, str]] = None,
        timeout: float = 30.0,
    ) -> Response:
        self.requests.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers or {}),
                "json_body": json_body,
                "form_body": form_body,
            }
        )
        for needle, route in self.routes:
            if needle in url:
                if route.json_body is not None:
                    body = _json.dumps(route.json_body).encode("utf-8")
                else:
                    body = route.text.encode("utf-8")
                return Response(status=route.status, headers=route.headers, body=body, url=url, set_cookies=list(route.set_cookies))
        return Response(status=404, headers={}, body=b"{}", url=url)
