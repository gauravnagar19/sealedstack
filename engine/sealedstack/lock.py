"""The remote state lock (fixes round 3 C1).

A lock object in the R2 state bucket, taken with a **conditional put**
(``If-None-Match: *``) and held for the whole run. Break-glass and CI write jobs
take the same lock, so only one writer can proceed; a stale lock needs
``--force-unlock <id>``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Optional

LOCK_KEY = "sealedstack-state.lock"


class LockHeld(RuntimeError):
    def __init__(self, holder: Optional[dict]):
        self.holder = holder or {}
        super().__init__(
            "state lock is held"
            + (f" by {self.holder.get('owner', '?')} (id {self.holder.get('id', '?')}, "
               f"since {self.holder.get('created', '?')})" if self.holder else "")
        )


@dataclass
class MemoryBackend:
    """An in-memory backend for tests."""

    store: Optional[dict] = None

    def __post_init__(self):
        if self.store is None:
            self.store = {}

    def create(self, key: str, body: bytes) -> bool:
        if key in self.store:
            return False
        self.store[key] = body
        return True

    def read(self, key: str) -> Optional[bytes]:
        return self.store.get(key)

    def delete(self, key: str) -> None:
        self.store.pop(key, None)


class S3LockBackend:
    def __init__(self, client, key: str = LOCK_KEY):
        self.client = client
        self.key = key

    def create(self, key: str, body: bytes) -> bool:
        return self.client.put_if_absent(key, body)

    def read(self, key: str) -> Optional[bytes]:
        return self.client.get(key)

    def delete(self, key: str) -> None:
        self.client.delete(key)


class StateLock:
    def __init__(self, backend, *, owner: str, key: str = LOCK_KEY):
        self.backend = backend
        self.owner = owner
        self.key = key

    def holder(self) -> Optional[dict]:
        body = self.backend.read(self.key)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"id": "unknown", "owner": "unknown", "created": "unknown"}

    def acquire(self) -> dict:
        info = {
            "id": uuid.uuid4().hex,
            "owner": self.owner,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if not self.backend.create(self.key, json.dumps(info).encode("utf-8")):
            raise LockHeld(self.holder())
        return info

    def release(self, info: Mapping[str, str]) -> bool:
        current = self.holder()
        if current and current.get("id") == info.get("id"):
            self.backend.delete(self.key)
            return True
        return False

    def force_unlock(self, lock_id: str) -> bool:
        current = self.holder()
        if current and current.get("id") == lock_id:
            self.backend.delete(self.key)
            return True
        return False
