"""``drill`` (D-084, D-085): rehearse restoring the newest database backup, in an isolated throwaway container.

A backup you have never restored is a hope. The drill takes the newest dump of an app's managed database and restores as
much of it as the chosen level asks for, into a **separate** Postgres container (no network, memory, CPU and process
limits, removed when the drill ends), never into the live instance. Nothing of the live database is read except its size.

Levels, each includes the ones before it:

* ``verify``  the gzip checksum over the whole file and the archive's table of contents (no database, no disk);
* ``schema``  the structure applies on this Postgres version, with its extensions and locale (no rows) (default);
* ``sample``  the first rows of every table load, with constraints and triggers switched off (types, encoding);
* ``tables``  the full data of the tables the app names, counted against ``min_rows``;
* ``full``    the complete restore, constraints and triggers included, behind a free-space check.

Places (``where``): ``server`` (default) runs the container on the app's server; the backup flows R2 -> server through a
presigned URL (``Db.stream_to``) and never through the machine the engine runs on. ``runner`` is structure only (levels
verify and schema): the server turns the backup into DDL text (no rows) and the engine's own machine applies it.

Before anything starts a resource preflight measures the host; a host that cannot take the drill gives the outcome
``skipped``, never ``failed``. Every report says what it did **not** test, so a green result is never read as more than it is.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

LEVELS = ("verify", "schema", "sample", "tables", "full")
PLACES = ("server", "runner")
DEFAULT_LEVEL = "schema"
DEFAULT_WHERE = "server"
DEFAULT_RATIO = 1.2
DEFAULT_MEMORY = "1g"
DEFAULT_CPUS = "1"
DEFAULT_TIMEOUT = "30m"
DEFAULT_MAX_AGE = "48h"
DEFAULT_MAX_LOAD = 0.7
DEFAULT_MIN_ROWS = 1
DEFAULT_SAMPLE_ROWS = 1
SMALL_DISK = 1024**3  # schema and sample need almost no space: a bare Postgres cluster
RC_TIMEOUT = 124  # `timeout`'s exit status, used for every step that ran out of time

NOT_TESTED = {
    "verify": "that the structure applies, that any data loads, disk and time",
    "schema": "that any data loads (types, constraints, triggers), disk and time",
    "sample": "constraints and triggers (off for the sample), the rest of each table, disk and time",
    "tables": "tables that were not named, constraints and triggers across tables, disk and time for the whole database",
    "full": "",
}
RUNNER_NOT_TESTED = "the server's own CPU, memory and disk (the structure was applied on the runner)"

# first lines of a table's data are `COPY ... FROM stdin;`; keep that line, n rows and the `\.` terminator
SAMPLE_AWK = r"""BEGIN { print "SET session_replication_role = replica;"; state = 0 }
state == 0 { print; if ($0 ~ /^COPY /) { state = 1; kept = 0 }; next }
state == 1 { if ($0 == "\\.") { print; state = 0 } else if (kept < n) { print; kept++ }; next }
"""

# One shell script, key=value lines out. Placeholders (not an f-string: docker's format strings have braces) are
# __IMAGE__ (quoted) and __TIMEOUT__ (seconds). The sweep removes labelled containers older than the timeout (the name
# ends in the unix time they were started) and counts the younger ones as busy.
PREFLIGHT = r"""
echo "cores=$(nproc 2>/dev/null || echo 1)"
echo "load1=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)"
echo "mem_avail=$(awk '/^MemAvailable:/ { print $2 * 1024 }' /proc/meminfo 2>/dev/null)"
root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
if [ -z "$root" ]; then echo "docker=down"; exit 0; fi
echo "docker=ok"
[ -d "$root" ] || root=/
echo "disk_free=$(df -P -B1 "$root" 2>/dev/null | awk 'NR == 2 { print $4 }')"
if docker image inspect __IMAGE__ >/dev/null 2>&1; then echo "image=present"; else echo "image=missing"; fi
busy=0; removed=0; now="$(date +%s)"
for entry in $(docker ps -a --filter label=sealedstack.drill=1 --format '{{.ID}}={{.Names}}' 2>/dev/null); do
  id="${entry%%=*}"; started="${entry##*-}"
  case "$started" in ''|*[!0-9]*) started=0 ;; esac
  if [ $((now - started)) -gt __TIMEOUT__ ]; then
    docker rm -fv "$id" >/dev/null 2>&1 && removed=$((removed + 1))
  else
    busy=$((busy + 1))
  fi
done
echo "busy=$busy"
echo "removed=$removed"
"""


class DrillError(RuntimeError):
    pass


class _Deadline(DrillError):
    pass


_TABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*(\.[A-Za-z_][A-Za-z0-9_$]*)?$")
_DURATION = re.compile(r"^(\d+)([smhd])$")
_SIZE = re.compile(r"^(\d+)([bkmg]?)b?$", re.IGNORECASE)
_CPUS = re.compile(r"^\d+(\.\d+)?$")


def parse_duration(value, what: str = "duration") -> int:
    """Seconds of ``90s``, ``30m``, ``2h`` or ``2d``."""
    match = _DURATION.match(str(value).strip())
    if not match:
        raise DrillError(f"{what} '{value}' is not a duration (like 90s, 30m, 2h, 48h)")
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def parse_size(value, what: str = "size") -> int:
    """Bytes of a docker memory value such as ``512m`` or ``1g``."""
    match = _SIZE.match(str(value).strip())
    if not match:
        raise DrillError(f"{what} '{value}' is not a size (like 512m, 1g)")
    return int(match.group(1)) * {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]


@dataclass
class Settings:
    level: str = DEFAULT_LEVEL
    where: str = DEFAULT_WHERE
    tables: list = field(default_factory=list)
    min_rows: int = DEFAULT_MIN_ROWS
    sample_rows: int = DEFAULT_SAMPLE_ROWS
    min_free_ratio: float = DEFAULT_RATIO
    memory: str = DEFAULT_MEMORY
    cpus: str = DEFAULT_CPUS
    timeout: int = 1800  # seconds
    max_load: float = DEFAULT_MAX_LOAD
    allow_image_pull: bool = False
    max_backup_age: int = 48 * 3600  # seconds


def settings_for(stack: dict, app_entry: dict, *, level: str = "", where: str = "", tables=()) -> Settings:
    """Arguments win over the app's ``drill:`` block in apps.yaml, which wins over the stack's ``drill:`` block."""
    base, mine = (stack.get("drill") or {}), (app_entry.get("drill") or {})
    limits = base.get("limits") or {}
    chosen = level or mine.get("level") or base.get("level") or DEFAULT_LEVEL
    if chosen not in LEVELS:
        raise DrillError(f"unknown drill level '{chosen}' (one of {', '.join(LEVELS)})")
    place = where or mine.get("where") or base.get("where") or DEFAULT_WHERE
    if place not in PLACES:
        raise DrillError(f"unknown drill place '{place}' (one of {', '.join(PLACES)})")
    if place == "runner" and chosen not in ("verify", "schema"):
        raise DrillError(
            f"where: runner is structure only (levels verify and schema), not '{chosen}': rows never leave the server. "
            "Use where: server for the data levels"
        )
    named = [str(t) for t in (tables or mine.get("tables") or [])]
    for table in named:
        if not _TABLE.match(table):
            raise DrillError(f"'{table}' is not a table name (letters, digits, _ and an optional schema.)")
    memory, cpus = str(limits.get("memory", DEFAULT_MEMORY)), str(limits.get("cpus", DEFAULT_CPUS))
    parse_size(memory, "limits.memory")
    if not _CPUS.match(cpus):
        raise DrillError(f"limits.cpus '{cpus}' is not a number of CPUs (like 1 or 0.5)")
    return Settings(
        level=chosen, where=place, tables=named,
        min_rows=int(mine.get("min_rows", DEFAULT_MIN_ROWS)), sample_rows=int(mine.get("sample_rows", DEFAULT_SAMPLE_ROWS)),
        min_free_ratio=float(base.get("min_free_ratio", DEFAULT_RATIO)), memory=memory, cpus=cpus,
        timeout=parse_duration(limits.get("timeout", DEFAULT_TIMEOUT), "limits.timeout"),
        max_load=float(limits.get("max_load", DEFAULT_MAX_LOAD)),
        allow_image_pull=bool(limits.get("allow_image_pull", False)),
        max_backup_age=parse_duration(base.get("max_backup_age", DEFAULT_MAX_AGE), "max_backup_age"),
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


@dataclass
class Outcome:
    app: str
    level: str
    where: str
    time: str = ""
    outcome: str = "passed"  # passed | failed | skipped
    backup: str = ""
    backup_age_hours: Optional[float] = None
    reason: str = ""
    tested: list = field(default_factory=list)
    not_tested: str = ""
    warnings: list = field(default_factory=list)
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "app": self.app, "time": self.time, "level": self.level, "where": self.where, "outcome": self.outcome,
            "backup": self.backup, "backup_age_hours": self.backup_age_hours, "reason": self.reason,
            "tested": list(self.tested), "not_tested": self.not_tested, "warnings": list(self.warnings),
            "duration_s": self.duration_s,
        }


def exit_code(outcome: Outcome) -> int:
    """0 for passed and skipped (a skip is a warning annotation), 1 for failed."""
    return 1 if outcome.outcome == "failed" else 0


def _annotation(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def print_report(outcome: Outcome, out=None, github_actions: bool = False) -> None:
    out = out if out is not None else sys.stdout
    head = {"passed": "ok", "failed": "FAILED", "skipped": "SKIPPED"}[outcome.outcome]
    print(f"==> drill {head} for {outcome.app}: level {outcome.level}, where {outcome.where}", file=out)
    if outcome.backup:
        age = f", {outcome.backup_age_hours} h old" if outcome.backup_age_hours is not None else ""
        print(f"    backup:     {outcome.backup}{age}", file=out)
    if outcome.reason:
        print(f"    reason:     {outcome.reason}", file=out)
    for line in outcome.tested:
        print(f"    tested:     {line}", file=out)
    if outcome.not_tested:
        print(f"    not tested: {outcome.not_tested}", file=out)
    for line in outcome.warnings:
        print(f"    warning:    {line}", file=out)
    if github_actions and outcome.outcome == "skipped":
        print(f"::warning title=backup check skipped::{_annotation(f'{outcome.app}: {outcome.reason}')}", file=out)
    elif github_actions and outcome.outcome == "failed":
        print(f"::error title=backup check failed::{_annotation(f'{outcome.app}: {outcome.reason}')}", file=out)


class ServerHost:
    """Shell scripts on the app's server over ssh (``bash -o pipefail -c``), with an optional ``timeout``."""

    def __init__(self, db, server: str):
        self.db, self.server = db, server

    def run(self, script: str, stdin: Optional[bytes] = None, timeout_s: int = 0) -> tuple:
        command = f"bash -o pipefail -c {shlex.quote(script)}"
        if timeout_s:
            command = f"timeout {int(timeout_s)} {command}"
        run = getattr(self.db.t, "ssh_full", None)
        if run is not None:
            return run(self.server, command, stdin)
        rc, out = self.db.t.ssh(self.server, command, stdin)
        return rc, out, b""


class RunnerHost:
    """The same scripts on the machine the engine runs on (it has the docker CLI and the host's Docker socket)."""

    def run(self, script: str, stdin: Optional[bytes] = None, timeout_s: int = 0) -> tuple:
        try:
            proc = subprocess.run(["bash", "-o", "pipefail", "-c", script], input=stdin, capture_output=True,
                                  timeout=timeout_s or None)
        except subprocess.TimeoutExpired:
            return RC_TIMEOUT, b"", b"timed out"
        except OSError as exc:
            return 127, b"", str(exc).encode()
        return proc.returncode, proc.stdout, proc.stderr


def _why(err, out=b"") -> str:
    lines = (err or out or b"").decode("utf-8", "replace").strip().splitlines()
    return ": " + " | ".join(line.strip() for line in lines[:4]) if lines else ""


def _probe(output: bytes) -> dict:
    info = {}
    for line in output.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key.strip()] = value.strip()
    return info


def _number(info: dict, key: str) -> Optional[float]:
    try:
        return float(info[key])
    except (KeyError, ValueError):
        return None  # unknown (e.g. no /proc): never a reason to skip


class Drill:
    def __init__(self, db, out=None, sleep=time.sleep, runner_host=None, server_host=None, clock=time.monotonic):
        self.db = db
        self.out = out if out is not None else sys.stdout
        self.sleep = sleep
        self.runner_host = runner_host
        self.server_host = server_host
        self.clock = clock
        self._deadline = 0.0

    # -- steps, all under one deadline ---------------------------------------
    def _left(self) -> int:
        left = int(self._deadline - self.clock())
        if left <= 0:
            raise _Deadline("the drill ran past its deadline")
        return left

    def _check(self, rc: int, err: bytes, out: bytes, what: str) -> None:
        if rc == RC_TIMEOUT or self._deadline - self.clock() <= 0:
            raise _Deadline(f"{what} ran past the drill's deadline")
        if rc != 0:
            raise DrillError(f"{what} failed{_why(err, out)}")

    def _shell(self, host, script: str, what: str, stdin: Optional[bytes] = None) -> bytes:
        rc, out, err = host.run(script, stdin, self._left())
        self._check(rc, err, out, what)
        return out

    def _stream(self, server: str, consumer: str, key: str, what: str, *, pipefail: bool = True) -> bytes:
        rc, out, err = self.db.stream_to(server, consumer, key, timeout_s=self._left(), pipefail=pipefail)
        self._check(rc, err, out, what)
        return out

    # -- the drill ------------------------------------------------------------
    def run(self, app: str, *, level: str = "", where: str = "", tables=()) -> Outcome:
        db = self.db
        entry = (db.config.apps.get("apps") or {}).get(app) or {}
        if not db.db_url(app):
            raise DrillError(f"'{app}' has no managed database")
        s = settings_for(db.config.stack, entry, level=level, where=where, tables=tables)
        server, image = db.primary_server(app), db.pg_image(app)
        if not server:
            raise DrillError(f"'{app}' has no server in apps.yaml")

        began = self.clock()
        self._deadline = began + s.timeout
        outcome = Outcome(app=app, level=s.level, where=s.where, time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        try:
            self._drill(app, s, server, image, outcome)
        except _Deadline as exc:
            outcome.outcome, outcome.reason = "failed", f"{exc} ({s.timeout} s limit, limits.timeout)"
        except DrillError as exc:
            outcome.outcome, outcome.reason = "failed", str(exc)
        if outcome.outcome == "passed":
            outcome.not_tested = NOT_TESTED[s.level] + (
                ("; " if NOT_TESTED[s.level] else "") + RUNNER_NOT_TESTED if s.where == "runner" else "")
        elif outcome.outcome == "skipped":
            outcome.not_tested = "everything: the drill did not start"
        else:
            outcome.not_tested = "everything after the failed step"
        outcome.duration_s = round(self.clock() - began, 1)
        self._record(outcome)
        return outcome

    def _record(self, outcome: Outcome) -> None:
        try:
            from . import drillrecord

            drillrecord.record(self.db.config, self.db.env, outcome.to_dict())
        except Exception:  # noqa: BLE001 - best effort: a drill result is never lost to its bookkeeping
            pass

    def _drill(self, app: str, s: Settings, server: str, image: str, outcome: Outcome) -> None:
        db = self.db
        key, size, stamp = db.latest_backup_entry(app)
        if not key:
            outcome.outcome, outcome.reason = "failed", (
                f"no backups of '{app}' under s3://{db.bucket}/ (looked for */{app}/{db.db_name(app)}/)")
            return
        outcome.backup = key
        if stamp:
            try:
                taken = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - taken).total_seconds()
                outcome.backup_age_hours = round(age / 3600, 1)
                if age > s.max_backup_age:
                    outcome.warnings.append(
                        f"the newest backup is {outcome.backup_age_hours} hours old (older than max_backup_age); "
                        "a drill of a stale backup proves little. Is the backup schedule running?")
            except ValueError:
                pass
        print(f"==> {app}: level {s.level}, where {s.where}; backup {key} ({size} bytes)", file=self.out)

        server_host = self.server_host or ServerHost(db, server)
        runner_host = (self.runner_host or RunnerHost()) if s.where == "runner" else None
        container_host = runner_host if s.where == "runner" else server_host
        container_level = LEVELS.index(s.level) >= LEVELS.index("schema")

        reason = self._preflight(app, s, image, server_host, runner_host if container_level else None, container_level, outcome)
        if reason:
            outcome.outcome, outcome.reason = "skipped", reason
            return

        # the backup is intact and readable (on the server, through the stream)
        self._stream(server, "cat > /dev/null", key, "checking the backup's gzip checksum")
        outcome.tested.append(f"gzip checksum of the whole file ({size} bytes compressed)")
        listing = self._stream(server, f"docker run --rm -i --network none {shlex.quote(image)} pg_restore --list", key,
                               "reading the archive contents", pipefail=False).decode("utf-8", "replace")
        toc = parse_toc(listing)
        if not toc["entries"]:
            raise DrillError("the archive has no contents")
        outcome.tested.append(f"archive contents ({toc['entries']} entries, {len(toc['data_tables'])} tables with data)")
        if not container_level:
            return

        name = f"sealedstack-drill-{int(time.time())}"
        try:
            self._start(container_host, name, image, s)
            if s.where == "runner":
                self._structure_from_server(server, runner_host, name, image, key, outcome)
            else:
                self._restore_on_server(server, name, key, s, outcome)
        finally:
            try:  # always, also after a failure or the deadline: it gets its own short allowance
                container_host.run(f"docker stop {name} >/dev/null 2>&1; docker rm -fv {name} >/dev/null 2>&1; true", None, 60)
            except Exception:  # noqa: BLE001
                pass

    # -- preflight ------------------------------------------------------------
    def _preflight(self, app, s: Settings, image, server_host, runner_host, container_level, outcome) -> str:
        """The first reason a host cannot take the drill, or "" (then nothing has been started)."""
        script = PREFLIGHT.replace("__IMAGE__", shlex.quote(image)).replace("__TIMEOUT__", str(int(s.timeout)))
        need_disk = 0
        if container_level:
            need_disk = SMALL_DISK
            if LEVELS.index(s.level) >= LEVELS.index("tables"):
                need_disk = int(self._live_size(app, outcome) * s.min_free_ratio)
        hosts = [("server", server_host, s.where == "server" and container_level)]
        if runner_host is not None:
            hosts.append(("runner", runner_host, True))
        for role, host, holds_container in hosts:
            rc, out, err = host.run(script, None, self._left())
            self._check(rc, err, out, f"measuring the {role}")
            info = _probe(out)
            if info.get("removed", "0") not in ("", "0"):
                outcome.warnings.append(f"removed {info['removed']} leftover drill container(s) older than the timeout on the {role}")
            reason = self._judge(info, s, role, holds_container, need_disk, image)
            if reason:
                return reason
        return ""

    def _live_size(self, app, outcome) -> int:
        try:
            return int(self.db.psql(app, self.db.db_url(app), "SELECT pg_database_size(current_database())", value=True) or 0)
        except Exception as exc:  # noqa: BLE001
            outcome.warnings.append(f"could not read the live database size ({exc}); the disk check used 0 bytes")
            return 0

    def _judge(self, info: dict, s: Settings, role: str, holds_container: bool, need_disk: int, image: str) -> str:
        on = f"on the {role}"
        if info.get("docker") != "ok":
            return f"docker does not answer {on}"
        if int(_number(info, "busy") or 0) > 0:
            return f"another drill container is already running {on} ({info['busy']}); one at a time"
        if role == "server" and info.get("image") == "missing" and not s.allow_image_pull:
            return f"the Postgres image {image} is not on the server; pull it, or set drill.limits.allow_image_pull: true"
        mem = _number(info, "mem_avail")
        if holds_container and mem is not None and mem < 2 * parse_size(s.memory):
            return f"not enough free memory {on}: {int(mem)} bytes available, {2 * parse_size(s.memory)} needed (2 x limits.memory {s.memory})"
        load, cores = _number(info, "load1"), _number(info, "cores") or 1
        if role == "server" and load is not None and load / cores > s.max_load:
            return f"the server is busy: load {load} on {int(cores)} core(s) is above limits.max_load {s.max_load}"
        free = _number(info, "disk_free")
        if holds_container and free is not None and free < need_disk:
            return f"not enough free disk space {on}: {int(free)} bytes free, {need_disk} needed for the {s.level} level"
        return ""

    # -- the container --------------------------------------------------------
    def _start(self, host, name: str, image: str, s: Settings) -> None:
        q = shlex.quote
        self._shell(
            host,
            f"docker run -d --rm --label sealedstack.drill=1 --name {name} --network none --memory {q(s.memory)} "
            f"--memory-swap {q(s.memory)} --cpus {q(s.cpus)} --cpu-shares 256 --pids-limit 256 -e POSTGRES_PASSWORD=drill "
            f"-e POSTGRES_HOST_AUTH_METHOD=trust {q(image)}",
            "starting the throwaway Postgres",
        )
        # over TCP: the image's init-phase server only listens on its socket and does not count
        for _ in range(45):
            rc, out, err = host.run(f"docker exec {name} pg_isready -h 127.0.0.1 -U postgres -q", None, self._left())
            if rc == 0:
                break
            if rc == RC_TIMEOUT:
                raise _Deadline("waiting for the throwaway Postgres ran past the drill's deadline")
            self.sleep(2)  # not ready yet (pg_isready answers 1 or 2); the deadline is checked by the next call
        else:
            raise DrillError("the throwaway Postgres did not become ready in 90 s")
        self._shell(host, f"docker exec {name} createdb -U postgres drill", "creating the throwaway database")

    def _count(self, host, name: str, sql: str) -> str:
        out = self._shell(host, f"docker exec {name} psql -U postgres -d drill -tA -c {shlex.quote(sql)}", "a check query")
        return out.decode("utf-8", "replace").strip() or "0"

    def _structure_from_server(self, server, runner_host, name, image, key, outcome) -> None:
        """Runner place: only DDL text (tables, columns, indexes: no rows) leaves the server."""
        ddl = self._stream(
            server, f"docker run --rm -i --network none {shlex.quote(image)} pg_restore --schema-only --no-owner --no-acl -f -",
            key, "producing the structure (DDL) on the server")
        self._shell(runner_host, f"docker exec -i {name} psql -U postgres -d drill -v ON_ERROR_STOP=1 -q",
                    "applying the structure on the runner", ddl)
        tables = self._count(runner_host, name, "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
        outcome.tested.append(f"the structure applies on this Postgres version ({tables} tables); only DDL text, no rows, reached the runner")

    def _restore_on_server(self, server, name, key, s: Settings, outcome) -> None:
        host = self.server_host or ServerHost(self.db, server)
        exec_ = f"docker exec -i {name}"
        if s.level == "full":
            self._stream(server, f"{exec_} pg_restore -U postgres -d drill --no-owner --no-acl", key, "the full restore")
            outcome.tested.append("the complete restore, constraints and triggers included")
            for table in s.tables:
                self._check_rows(host, name, s, table, outcome)
            return
        self._stream(server, f"{exec_} pg_restore -U postgres -d drill --schema-only --no-owner --no-acl", key,
                     "restoring the structure")
        tables = self._count(host, name, "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
        outcome.tested.append(f"the structure applies on this Postgres version ({tables} tables)")
        if s.level == "sample":
            self._shell(host, f"{exec_} sh -c 'cat > /tmp/sample.awk'", "preparing the sample", SAMPLE_AWK.encode())
            pipeline = (
                f"pg_restore --data-only --no-owner --no-acl -f - | awk -v n={int(s.sample_rows)} -f /tmp/sample.awk "
                "| psql -U postgres -d drill -v ON_ERROR_STOP=1 -q"
            )
            self._stream(server, f"{exec_} sh -c {shlex.quote(pipeline)}", key, "loading the sampled rows")
            outcome.tested.append(f"the first {s.sample_rows} row(s) of every table load (constraints and triggers off for the sample)")
        elif s.level == "tables":
            if not s.tables:
                outcome.tested.append("(no tables named for this app: set drill.tables in apps.yaml, or pass a table, to test data)")
            for table in s.tables:
                self._stream(
                    server,
                    f"docker exec -i -e PGOPTIONS='-c session_replication_role=replica' {name} pg_restore -U postgres -d drill "
                    f"--data-only --no-owner --no-acl -t {shlex.quote(table)}",
                    key, f"restoring the data of {table}")
                self._check_rows(host, name, s, table, outcome)

    def _check_rows(self, host, name, s: Settings, table: str, outcome) -> None:
        rows = int(self._count(host, name, f"SELECT count(*) FROM {table}"))
        if rows < s.min_rows:
            raise DrillError(f"{table} holds {rows} row(s) after the restore, fewer than min_rows {s.min_rows}")
        outcome.tested.append(f"the full data of {table} ({rows} rows, at least {s.min_rows} expected)")
