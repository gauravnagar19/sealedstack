"""Where ``up`` remembers which phases finished (so a re-run resumes, C3 / G10).

CI jobs start on a fresh runner, so a record in ``.sealedstack/`` is lost between
runs. The record is therefore kept in the R2 state bucket next to the state lock,
and mirrored to ``.sealedstack/`` for local runs. It is tied to the commit it was
made for: after a config change a stale record is ignored, so changed phases run
again. It holds phase names only, nothing secret.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


class FileStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> Optional[dict]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, record: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(record), encoding="utf-8")
        except OSError:
            pass

    def clear(self) -> None:
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


class S3Store:
    def __init__(self, client, key: str) -> None:
        self.client, self.key = client, key

    def load(self) -> Optional[dict]:
        try:
            body = self.client.get(self.key)
            return json.loads(body.decode("utf-8")) if body else None
        except Exception:  # noqa: BLE001 - an unreadable record means "start over", never "fail"
            return None

    def save(self, record: dict) -> None:
        try:
            self.client.put(self.key, json.dumps(record).encode("utf-8"))
        except Exception:  # noqa: BLE001
            pass

    def clear(self) -> None:
        try:
            self.client.delete(self.key)
        except Exception:  # noqa: BLE001
            pass


class Progress:
    """Remote record first, local mirror second; both are best effort."""

    def __init__(self, stores, *, commit: str = "") -> None:
        self.stores = [s for s in stores if s is not None]
        self.commit = commit

    def load(self) -> tuple[set, str]:
        """``(finished phases, note)``; a record for another commit is ignored."""
        for store in self.stores:
            record = store.load()
            if not isinstance(record, dict):
                continue
            if self.commit and record.get("commit") and record["commit"] != self.commit:
                return set(), f"ignoring the progress recorded for commit {str(record['commit'])[:7]} (this run is {self.commit[:7]})"
            return set(record.get("done") or []), ""
        return set(), ""

    def save(self, done: set) -> None:
        record = {"commit": self.commit, "done": sorted(done)}
        for store in self.stores:
            store.save(record)

    def clear(self) -> None:
        for store in self.stores:
            store.clear()
