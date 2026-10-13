"""Tests never touch the network: a real (non-loopback) HTTP request fails loudly."""
from __future__ import annotations

import urllib.request

import pytest

_real_urlopen = urllib.request.urlopen


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch):
    def guarded(req, *args, **kwargs):
        url = req if isinstance(req, str) else getattr(req, "full_url", "")
        if url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]")):
            return _real_urlopen(req, *args, **kwargs)
        pytest.fail(f"a test tried to make a real network request to {url[:60]} (inject a StubClient)")

    monkeypatch.setattr(urllib.request, "urlopen", guarded)
    yield
