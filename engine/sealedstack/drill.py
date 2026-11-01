"""``drill`` (D-084): rehearse restoring the newest database backup, in an isolated throwaway container.

A backup you have never restored is a hope. The drill takes the newest dump of an app's managed database and restores as
much of it as the chosen level asks for, into a **separate** Postgres container on the app's server (no network, a memory
limit, removed when the drill ends), never into the live instance. Nothing of the live database is read except its size.

Levels, each includes the ones before it:

* ``verify``  the gzip checksum over the whole file and the archive's table of contents (no database, no disk);
* ``schema``  the structure applies on this Postgres version, with its extensions and locale (no rows);
* ``sample``  the first rows of every table load, with constraints and triggers switched off (types, encoding);
* ``tables``  the full data of the tables the app names, counted against ``min_rows`` (default);
* ``full``    the complete restore, constraints and triggers included, behind a free-space check.

Every report says what it did **not** test, so a green result is never read as more than it is.
"""
from __future__ import annotations

import gzip
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

LEVELS = ("verify", "schema", "sample", "tables", "full")
DEFAULT_LEVEL = "tables"
DEFAULT_RATIO = 1.2
DEFAULT_MEMORY = "1g"
DEFAULT_MIN_ROWS = 1
DEFAULT_SAMPLE_ROWS = 1
MAX_DUMP_BYTES = 2 * 1024**3  # the dump is held in memory and piped over ssh; larger ones need the streaming follow-up

NOT_TESTED = {
    "verify": "that the structure applies, that any data loads, disk and time",
    "schema": "that any data loads (types, constraints, triggers), disk and time",
    "sample": "constraints and triggers (off for the sample), the rest of each table, disk and time",
    "tables": "tables that were not named, constraints and triggers across tables, disk and time for the whole database",
    "full": "",
}

# first lines of a table's data are `COPY ... FROM stdin;`; keep that line, n rows and the `\.` terminator
SAMPLE_AWK = r"""BEGIN { print "SET session_replication_role = replica;"; state = 0 }
state == 0 { print; if ($0 ~ /^COPY /) { state = 1; kept = 0 }; next }
state == 1 { if ($0 == "\\.") { print; state = 0 } else if (kept < n) { print; kept++ }; next }
"""

_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")


class DrillError(RuntimeError):
    pass


@dataclass
class Settings:
    level: str = DEFAULT_LEVEL
    tables: list = field(default_factory=list)
    min_rows: int = DEFAULT_MIN_ROWS
    sample_rows: int = DEFAULT_SAMPLE_ROWS
    min_free_ratio: float = DEFAULT_RATIO
    memory: str = DEFAULT_MEMORY


def settings_for(stack: dict, app_entry: dict, *, level: str = "", tables=()) -> Settings:
    """Arguments win over the app's ``drill:`` block in apps.yaml, which wins over the stack's ``drill:`` block."""
    base, mine = (stack.get("drill") or {}), (app_entry.get("drill") or {})
    chosen = level or mine.get("level") or base.get("level") or DEFAULT_LEVEL
    if chosen not in LEVELS:
        raise DrillError(f"unknown drill level '{chosen}' (one of {', '.join(LEVELS)})")
    named = [str(t) for t in (tables or mine.get("tables") or [])]
    for table in named:
        if not _TABLE.match(table):
            raise DrillError(f"'{table}' is not a table name (letters, digits, _ and an optional schema.)")
    return Settings(
        level=chosen, tables=named,
        min_rows=int(mine.get("min_rows", DEFAULT_MIN_ROWS)), sample_rows=int(mine.get("sample_rows", DEFAULT_SAMPLE_ROWS)),
        min_free_ratio=float(base.get("min_free_ratio", DEFAULT_RATIO)), memory=str(base.get("memory", DEFAULT_MEMORY)),
    )


def parse_toc(listing: str) -> dict:
    """Entries of ``pg_restore --list``: how many, and the tables that carry data."""
    entries, data = 0, []
    for line in listing.splitlines():
        if not line.strip() or line.startswith(";"):
            continue
        entries += 1
        match = re.match(r"^\d+;\s+\d+\s+\d+\s+TABLE DATA\s+(\S+)\s+(\S+)", line)
        if match:
            data.append(f"{match.group(1)}.{match.group(2)}")
    return {"entries": entries, "data_tables": data}


class Drill:
    def __init__(self, db, out=None, sleep=time.sleep):
        self.db = db
        self.out = out if out is not None else sys.stdout
        self.sleep = sleep

    # -- ssh helpers ----------------------------------------------------------
    def _ssh(self, server: str, command: str, stdin: Optional[bytes] = None) -> tuple:
        run = getattr(self.db.t, "ssh_full", None)
        if run is not None:
            return run(server, command, stdin)
        rc, out = self.db.t.ssh(server, command, stdin)
        return rc, out, b""

    def _must(self, server: str, command: str, what: str, stdin: Optional[bytes] = None) -> bytes:
        rc, out, err = self._ssh(server, command, stdin)
        if rc != 0:
            detail = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
            raise DrillError(f"{what} failed" + (f": {' | '.join(detail[:4])}" if detail else ""))
        return out

    # -- the drill ------------------------------------------------------------
    def run(self, app: str, *, level: str = "", tables=()) -> int:
        db = self.db
        entry = (db.config.apps.get("apps") or {}).get(app) or {}
        if not db.db_url(app):
            raise DrillError(f"'{app}' has no managed database")
        s = settings_for(db.config.stack, entry, level=level, tables=tables)
        server, image = db.primary_server(app), db.pg_image(app)
        if not server:
            raise DrillError(f"'{app}' has no server in apps.yaml")

        key, size = db.latest_backup_entry(app)
        if not key:
            raise DrillError(f"no backups of '{app}' under s3://{db.bucket}/ (looked for */{app}/{db.db_name(app)}/)")
        if size > MAX_DUMP_BYTES:
            raise DrillError(f"the newest backup is {size} bytes; drills hold the dump in memory up to {MAX_DUMP_BYTES}")
        print(f"==> {app}: level {s.level}; backup {key} ({size} bytes)", file=self.out)

        raw = db.s3_download(key)
        try:
            dump = gzip.decompress(raw)  # reads the whole file and checks its CRC
        except (OSError, EOFError) as exc:
            raise DrillError(f"the backup is not a complete gzip file: {exc}") from exc
        tested = [f"gzip checksum of the whole file ({len(dump)} bytes unpacked)"]

        listing = self._must(server, f"docker run --rm -i --network none {shlex.quote(image)} pg_restore --list",
                             "reading the archive contents", dump).decode("utf-8", "replace")
        toc = parse_toc(listing)
        if not toc["entries"]:
            raise DrillError("the archive has no contents")
        tested.append(f"archive contents ({toc['entries']} entries, {len(toc['data_tables'])} tables with data)")

        if LEVELS.index(s.level) >= LEVELS.index("schema"):
            self._with_container(server, image, s, app, dump, toc, tested)
        self._report(app, s, tested)
        return 0

    def _with_container(self, server, image, s: Settings, app, dump, toc, tested) -> None:
        if LEVELS.index(s.level) >= LEVELS.index("tables"):
            self._check_space(server, s, app, len(dump))
        name = f"sealedstack-drill-{int(time.time())}"
        q = shlex.quote
        self._must(
            server,
            f"docker run -d --rm --name {name} --network none --memory {q(s.memory)} -e POSTGRES_PASSWORD=drill "
            f"-e POSTGRES_HOST_AUTH_METHOD=trust {q(image)}",
            "starting the throwaway Postgres",
        )
        try:
            self._must(
                server,
                f"for i in $(seq 1 45); do docker exec {name} pg_isready -h 127.0.0.1 -U postgres -q && exit 0; sleep 2; done; exit 1",
                "waiting for the throwaway Postgres",
            )
            self._must(server, f"docker exec {name} createdb -U postgres drill", "creating the throwaway database")
            exec_ = f"docker exec -i {name}"
            if s.level == "full":
                self._must(server, f"{exec_} pg_restore -U postgres -d drill --no-owner --no-acl", "the full restore", dump)
                tested.append("the complete restore, constraints and triggers included")
            else:
                self._must(server, f"{exec_} pg_restore -U postgres -d drill --schema-only --no-owner --no-acl",
                           "restoring the structure", dump)
                tables = self._count(server, name, "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
                tested.append(f"the structure applies on this Postgres version ({tables} tables)")
                if s.level == "sample":
                    self._sample(server, name, s, dump, tested)
                if s.level == "tables":
                    self._tables(server, name, s, dump, tested)
            if s.level == "full":
                for table in s.tables:
                    self._check_rows(server, name, s, table, tested)
        finally:
            rc, _out, _err = self._ssh(server, f"docker stop {name} >/dev/null 2>&1; docker rm -fv {name} >/dev/null 2>&1; true")

    def _sample(self, server, name, s, dump, tested) -> None:
        self._must(server, f"docker exec -i {name} sh -c 'cat > /tmp/sample.awk'", "preparing the sample", SAMPLE_AWK.encode())
        pipeline = (
            f"pg_restore --data-only --no-owner --no-acl -f - | awk -v n={int(s.sample_rows)} -f /tmp/sample.awk "
            "| psql -U postgres -d drill -v ON_ERROR_STOP=1 -q"
        )
        self._must(server, f"docker exec -i {name} sh -c {shlex.quote(pipeline)}", "loading the sampled rows", dump)
        tested.append(f"the first {s.sample_rows} row(s) of every table load (constraints and triggers off for the sample)")

    def _tables(self, server, name, s, dump, tested) -> None:
        if not s.tables:
            tested.append("(no tables named for this app: set drill.tables in apps.yaml, or pass a table, to test data)")
            return
        for table in s.tables:
            self._must(
                server,
                f"docker exec -i -e PGOPTIONS='-c session_replication_role=replica' {name} pg_restore -U postgres -d drill "
                f"--data-only --no-owner --no-acl -t {shlex.quote(table)}",
                f"restoring the data of {table}", dump,
            )
            self._check_rows(server, name, s, table, tested)

    def _check_rows(self, server, name, s, table, tested) -> None:
        rows = int(self._count(server, name, f"SELECT count(*) FROM {table}"))
        if rows < s.min_rows:
            raise DrillError(f"{table} holds {rows} row(s) after the restore, fewer than min_rows {s.min_rows}")
        tested.append(f"the full data of {table} ({rows} rows, at least {s.min_rows} expected)")

    def _count(self, server, name, sql) -> str:
        out = self._must(server, f"docker exec {name} psql -U postgres -d drill -tA -c {shlex.quote(sql)}", "a check query")
        return out.decode("utf-8", "replace").strip() or "0"

    def _check_space(self, server, s: Settings, app, dump_bytes: int) -> None:
        """Skip the data levels instead of risking the disk: needs `ratio` times the dump (tables) or the live database (full)."""
        need = dump_bytes
        if s.level == "full":
            url = self.db.db_url(app)
            live = self.db.psql(app, url, "SELECT pg_database_size(current_database())", value=True)
            need = max(need, int(live or 0))
        need = int(need * s.min_free_ratio)
        out = self._must(server, "df --output=avail -B1 \"$(docker info --format '{{.DockerRootDir}}')\" | tail -n 1", "reading the free disk space")
        free = int(out.decode().strip() or 0)
        if free < need:
            raise DrillError(
                f"not enough free disk space for the {s.level} level: {free} bytes free, {need} needed "
                f"({s.min_free_ratio} x {'the live database' if s.level == 'full' else 'the dump'}). Nothing was restored; "
                "a lower level (--level schema) needs almost no space"
            )

    def _report(self, app: str, s: Settings, tested: list) -> None:
        print(f"==> drill ok for {app}: level {s.level}", file=self.out)
        for line in tested:
            print(f"    tested:     {line}", file=self.out)
        if NOT_TESTED[s.level]:
            print(f"    not tested: {NOT_TESTED[s.level]}", file=self.out)
