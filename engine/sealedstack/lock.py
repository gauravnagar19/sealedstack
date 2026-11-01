"""The remote state lock (fixes round 3 C1, D-062).

A lock object at ``sealedstack/<infra name>/state.lock`` in the R2 state bucket,
taken with a **conditional put** (``If-None-Match: *``) and held for the whole
run. Break-glass and CI write jobs take the same lock, so only one writer can
proceed. The body is ``{id, owner, created, expires, run_url}``: a lock past its
``expires`` (default 2 hours, for a runner killed mid-apply) may be taken over
with a warning naming the previous holder; anything else needs
``--force-unlock <id>``. Release removes only the caller's own lock.
"""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping, Optional

DEFAULT_TTL = timedelta(hours=2)
STALE_AFTER = timedelta(hours=1)  # the Status issue reports a lock held longer than this
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_time(value) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value), _TIME_FORMAT).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def describe_age(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def run_url(env: Mapping[str, str]) -> str:
    """The CI run that holds the lock (empty outside GitHub Actions)."""
    server, repo, run = env.get("GITHUB_SERVER_URL", ""), env.get("GITHUB_REPOSITORY", ""), env.get("GITHUB_RUN_ID", "")
    return f"{server}/{repo}/actions/runs/{run}" if server and repo and run else ""


def lock_owner(env: Mapping[str, str], kind: str) -> str:
    """``ci:<actor>`` or ``break-glass:<user>``: who holds the lock, for the message."""
    who = env.get("GITHUB_ACTOR") or env.get("USER") or "unknown"
    return f"{kind}:{who}"

LOCK_KEY = "sealedstack/sealedstack/state.lock"  # fallback; real keys come from lock_key()


def lock_key(infra_name: str) -> str:
    """The lock object, namespaced per infra repo inside the state bucket (D-062)."""
    return f"sealedstack/{infra_name or 'sealedstack'}/state.lock"


class LockHeld(RuntimeError):
    def __init__(self, holder: Optional[dict], *, now: Optional[datetime] = None):
        self.holder = holder or {}
        created = _parse_time(self.holder.get("created"))
        age = describe_age((now or datetime.now(timezone.utc)) - created) if created else "unknown time"
        detail = ""
        if self.holder:
            detail = (
                f" by {self.holder.get('owner', '?')} (id {self.holder.get('id', '?')}, held for {age}"
                + (f", run {self.holder['run_url']}" if self.holder.get("run_url") else "")
                + (f", expires {self.holder['expires']}" if self.holder.get("expires") else "")
                + ")"
            )
        hint = f" If it is stale, clear it with `sealedstack --force-unlock {self.holder.get('id', '<id>')}`." if self.holder else ""
        super().__init__(f"state lock is held{detail}.{hint}")


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


class LockUnavailable(RuntimeError):
    """There is nowhere to keep the lock yet: the state bucket does not exist (first run, before `up`)."""


class S3LockBackend:
    def __init__(self, client, key: str = LOCK_KEY):
        self.client = client
        self.key = key

    def create(self, key: str, body: bytes) -> bool:
        from .s3 import S3BucketMissing

        try:
            return self.client.put_if_absent(key, body)
        except S3BucketMissing as exc:
            raise LockUnavailable(str(exc)) from exc

    def read(self, key: str) -> Optional[bytes]:
        return self.client.get(key)

    def delete(self, key: str) -> None:
        self.client.delete(key)


class StateLock:
    def __init__(
        self,
        backend,
        *,
        owner: str,
        key: str = LOCK_KEY,
        ttl: timedelta = DEFAULT_TTL,
        run_url: str = "",
        clock: Optional[Callable[[], datetime]] = None,
        warn: Optional[Callable[[str], None]] = None,
    ):
        self.backend = backend
        self.owner = owner
        self.key = key
        self.ttl = ttl
        self.run_url = run_url
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._warn = warn or (lambda text: print(f"sealedstack: {text}", file=sys.stderr))

    def holder(self) -> Optional[dict]:
        body = self.backend.read(self.key)
        if not body:
            return None
        try:
            data = json.loads(body.decode("utf-8"))
            return data if isinstance(data, dict) else {"id": "unknown", "owner": "unknown", "created": "unknown"}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"id": "unknown", "owner": "unknown", "created": "unknown"}

    def age(self, holder: Mapping[str, str]) -> Optional[timedelta]:
        created = _parse_time(holder.get("created"))
        return self._clock() - created if created else None

    def expired(self, holder: Mapping[str, str]) -> bool:
        expires = _parse_time(holder.get("expires"))
        return expires is not None and expires <= self._clock()

    def _new_info(self) -> dict:
        now = self._clock()
        return {
            "id": uuid.uuid4().hex,
            "owner": self.owner,
            "created": now.strftime(_TIME_FORMAT),
            "expires": (now + self.ttl).strftime(_TIME_FORMAT),
            "run_url": self.run_url,
        }

    def acquire(self) -> dict:
        info = self._new_info()
        if self.backend.create(self.key, json.dumps(info).encode("utf-8")):
            return info
        current = self.holder()
        if current is not None and self.expired(current):
            # A runner killed mid-apply never released it. Take it over, loudly.
            # Read-then-delete is not atomic: two takers racing for the same
            # expired lock could delete each other's fresh one. The window is a
            # few requests, the holder is already dead, and docs/limitations.md
            # says so; a conditional delete would close it.
            self._warn(
                f"taking over an expired state lock held by {current.get('owner', '?')} "
                f"(id {current.get('id', '?')}, expired {current.get('expires', '?')}"
                + (f", run {current['run_url']}" if current.get("run_url") else "")
                + ")"
            )
            again = self.holder()
            if again is not None and again.get("id") == current.get("id"):
                self.backend.delete(self.key)
            if self.backend.create(self.key, json.dumps(info).encode("utf-8")):
                return info
            current = self.holder()
        raise LockHeld(current, now=self._clock())

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

    def stale_report(self) -> str:
        """A problem text when the lock has been held longer than ``STALE_AFTER`` (else "")."""
        current = self.holder()
        if not current:
            return ""
        age = self.age(current)
        if age is None or age <= STALE_AFTER:
            return ""
        return (
            f"The state lock has been held for {describe_age(age)} by {current.get('owner', '?')} "
            f"(id {current.get('id', '?')}"
            + (f", run {current['run_url']}" if current.get("run_url") else "")
            + f"). If that run is gone, clear it with `sealedstack --force-unlock {current.get('id', '<id>')}`."
        )
