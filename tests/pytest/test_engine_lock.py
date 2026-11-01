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


class _FlakyBackend(lock.MemoryBackend):
    """Fails the first creates with the storage's own error, like R2's HTTP 500 in the live run 2026-11-01."""

    def __init__(self, failures: int, write_anyway: bool = False):
        super().__init__()
        self.failures, self.write_anyway, self.calls = failures, write_anyway, 0

    def create(self, key, body):
        self.calls += 1
        if self.calls <= self.failures:
            if self.write_anyway:  # the object was stored, only the answer was lost
                super().create(key, body)
            raise s3.S3Error(f"conditional put {key} failed (HTTP 500)")
        return super().create(key, body)


def _state(backend, warnings):
    return lock.StateLock(backend, owner="ci", warn=warnings.append, sleep=lambda seconds: None)


def test_a_transient_storage_error_is_retried_and_the_lock_is_taken():
    warnings = []
    backend = _FlakyBackend(failures=1)
    info = _state(backend, warnings).acquire()
    assert backend.calls == 2 and info["owner"] == "ci" and "HTTP 500" in warnings[0]


def test_a_lost_answer_does_not_make_us_a_stranger_to_our_own_lock():
    backend = _FlakyBackend(failures=1, write_anyway=True)
    info = _state(backend, []).acquire()
    assert backend.calls == 1  # no second create: reading the lock showed our own id
    assert _state(backend, []).holder()["id"] == info["id"]


def test_a_storage_that_keeps_failing_gives_up_with_its_error():
    backend = _FlakyBackend(failures=10)
    with pytest.raises(s3.S3Error):
        _state(backend, []).acquire()
    assert backend.calls == lock.CREATE_ATTEMPTS


def test_a_lock_held_by_someone_else_is_not_retried():
    backend = lock.MemoryBackend()
    lock.StateLock(backend, owner="alice").acquire()
    with pytest.raises(lock.LockHeld):
        _state(backend, []).acquire()
