"""C1 state lock (S3 conditional put) + the S3 client it uses."""
from __future__ import annotations

import pytest

from sealedstack import lock, s3
from sealedstack.http import Response


def test_state_lock_acquire_release_and_contention():
    backend = lock.MemoryBackend()
    a = lock.StateLock(backend, owner="alice")
    b = lock.StateLock(backend, owner="bob")

    info = a.acquire()
    assert a.holder()["owner"] == "alice"
    with pytest.raises(lock.LockHeld):
        b.acquire()

    # release is id-based: a wrong id does not free the lock
    assert b.release({"id": "wrong"}) is False
    assert a.release(info) is True
    assert a.holder() is None
    b.acquire()  # now free


def test_force_unlock_requires_the_id():
    backend = lock.MemoryBackend()
    state = lock.StateLock(backend, owner="alice")
    info = state.acquire()
    assert state.force_unlock("nope") is False
    assert state.force_unlock(info["id"]) is True
    assert state.holder() is None


class _FakeSender:
    def __init__(self):
        self.objects: dict = {}

    def __call__(self, method, url, headers, body):
        key = url.split("cloudflarestorage.com/", 1)[1]
        if method == "PUT":
            if headers.get("If-None-Match") and key in self.objects:
                return Response(status=412, body=b"", url=url)
            self.objects[key] = body
            return Response(status=200, body=b"", url=url)
        if method == "GET":
            if key in self.objects:
                return Response(status=200, body=self.objects[key], url=url)
            return Response(status=404, body=b"", url=url)
        if method == "DELETE":
            self.objects.pop(key, None)
            return Response(status=204, body=b"", url=url)
        return Response(status=400, url=url)


def test_s3_conditional_put_get_delete():
    sender = _FakeSender()
    client = s3.S3Client("https://acct.r2.cloudflarestorage.com", "sealedstack-state", "ak", "sk", sender=sender)

    assert client.put_if_absent("k", b"one") is True
    assert client.get("k") == b"one"
    assert client.put_if_absent("k", b"two") is False  # already exists -> 412
    assert client.get("k") == b"one"
    assert client.get("missing") is None
    client.delete("k")
    assert client.get("k") is None


def test_s3_requests_are_signed():
    captured = {}

    def sender(method, url, headers, body):
        captured.update(headers)
        return Response(status=200, body=b"", url=url)

    client = s3.S3Client("https://acct.r2.cloudflarestorage.com", "sealedstack-state", "AKIA", "secret", sender=sender)
    client.put_if_absent("k", b"x")
    assert captured["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIA/")
    assert "x-amz-date" in captured
