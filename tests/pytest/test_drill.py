"""D-085: the restore drill v2: levels, places, resource preflight, isolation, deadline, outcome."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
for extra in (REPO / "engine", REPO / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from sealedstack import drill  # noqa: E402

import validate as validate_mod  # noqa: E402

TOC = (
    ";\n; Archive created at 2026-11-01\n"
    "215; 1259 16385 TABLE public notes demo\n"
    "3260; 0 16385 TABLE DATA public notes demo\n"
    "3261; 0 16390 TABLE DATA public tags demo\n"
)
DDL = b"CREATE TABLE public.notes (id integer);\n"
KEY = "old-db-abc/shop/shop/2026-11-01T18-00-00Z.sql.gz"


def fresh_stamp(hours=3):
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")


def probe(**over):
    info = {"cores": 4, "load1": 0.2, "mem_avail": 8 * 1024**3, "docker": "ok", "disk_free": 10**12,
            "image": "present", "busy": 0, "removed": 0}
    info.update(over)
    return "".join(f"{k}={v}\n" for k, v in info.items()).encode()


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeHost:
    """A scripted host: records (script, stdin, timeout_s) and answers by what the script contains."""

    def __init__(self, clock=None, step=0, info=None, fail=None, rows="2"):
        self.calls, self.clock, self.step, self.fail, self.rows = [], clock, step, fail or {}, rows
        self.info = info if info is not None else probe()

    def run(self, script, stdin=None, timeout_s=0):
        self.calls.append((script, stdin, timeout_s))
        if self.clock:
            self.clock.now += self.step
        for needle, answer in self.fail.items():
            if needle in script:
                return answer
        if "docker info" in script:
            return 0, self.info, b""
        if "FROM pg_tables" in script:
            return 0, b"2", b""
        if "SELECT count(*) FROM notes" in script:
            return 0, self.rows.encode(), b""
        return 0, b"", b""

    def scripts(self):
        return [script for script, _stdin, _t in self.calls]


class FakeDb:
    """What Drill needs of Db, including the helpers db.py gains alongside this module."""

    def __init__(self, clock=None, step=0, fail=None, stamp=None, key=KEY, live=1000):
        self.config = SimpleNamespace(stack={}, apps={"apps": {"shop": {"servers": ["web-1"]}}}, config_dir=None)
        self.env, self.bucket = {}, "bucket"
        self.streams, self.psqls, self.clock, self.step, self.fail = [], [], clock, step, fail or {}
        self.entry = (key, 4096, stamp if stamp is not None else fresh_stamp()) if key else ("", 0, "")
        self.live = live
        self.t = SimpleNamespace(ssh_full=self._ssh_full)
        self.ssh_calls = []

    def _ssh_full(self, server, cmd, stdin=None):
        self.ssh_calls.append((server, cmd, stdin))
        return 0, b"", b""

    def db_url(self, app):
        return "postgres://u:p@db:5432/shop"

    def db_name(self, app):
        return "shop"

    def primary_server(self, app):
        return "web-1"

    def pg_image(self, app):
        return "postgres:16"

    def latest_backup_entry(self, app):
        return self.entry

    def psql(self, app, url, sql, value=False):
        self.psqls.append(sql)
        return str(self.live)

    def stream_to(self, server, consumer, key, *, timeout_s=0, pipefail=True):
        self.streams.append({"server": server, "consumer": consumer, "key": key, "timeout_s": timeout_s, "pipefail": pipefail})
        if self.clock:
            self.clock.now += self.step
        for needle, answer in self.fail.items():
            if needle in consumer:
                return answer
        if "pg_restore --list" in consumer:
            return 0, TOC.encode(), b""
        if "-f -" in consumer:
            return 0, DDL, b""
        return 0, b"", b""

    def consumers(self):
        return [s["consumer"] for s in self.streams]


def make(*, app_drill=None, stack_drill=None, **kw):
    db = FakeDb(**kw)
    if app_drill:
        db.config.apps["apps"]["shop"]["drill"] = app_drill
    if stack_drill:
        db.config.stack["drill"] = stack_drill
    return db


def drill_for(db, server=None, runner=None, clock=None):
    return drill.Drill(db, io.StringIO(), sleep=lambda _s: None, server_host=server or FakeHost(),
                       runner_host=runner or FakeHost(), clock=clock or (db.clock or Clock()))


def run(db, server=None, runner=None, **kw):
    server, runner = server or FakeHost(), runner or FakeHost()
    return drill_for(db, server, runner).run("shop", **kw), server, runner


def started(host):
    return [s for s in host.scripts() if "docker run -d" in s]


# -- levels ----------------------------------------------------------------------

def test_the_default_level_is_schema():
    db = make()
    outcome, server, _ = run(db)
    assert outcome.level == "schema" and outcome.where == "server" and outcome.outcome == "passed"
    assert any("--schema-only" in c for c in db.consumers()) and not any("--data-only" in c for c in db.consumers())
    assert db.psqls == []  # the live database is not even sized for the light levels


def test_verify_checks_the_checksum_and_the_contents_and_starts_nothing():
    db = make()
    outcome, server, _ = run(db, level="verify")
    assert outcome.outcome == "passed" and started(server) == []
    checksum, contents = db.streams
    assert checksum["consumer"] == "cat > /dev/null" and checksum["pipefail"] is True
    assert contents["consumer"] == "docker run --rm -i --network none postgres:16 pg_restore --list" and contents["pipefail"] is False
    assert any("archive contents (3 entries, 2 tables with data)" in t for t in outcome.tested)
    assert any("gzip checksum" in t for t in outcome.tested)


def test_a_crc_error_in_the_checksum_step_fails_the_drill():
    db = make(fail={"cat > /dev/null": (2, b"", b"gzip: stdin: crc error\n")})
    outcome, _, _ = run(db, level="verify")
    assert outcome.outcome == "failed" and "crc error" in outcome.reason and "checksum" in outcome.reason


def test_an_empty_archive_fails():
    db = make()
    db.stream_to = lambda *a, **k: (0, b";\n", b"")
    outcome, _, _ = run(db, level="verify")
    assert outcome.outcome == "failed" and "no contents" in outcome.reason


def test_the_container_is_isolated_and_limited():
    db = make()
    _, server, _ = run(db)
    start = started(server)[0]
    for flag in ("-d", "--rm", "--label sealedstack.drill=1", "--network none", "--memory 1g", "--memory-swap 1g", "--cpus 1",
                 "--cpu-shares 256", "--pids-limit 256", "POSTGRES_PASSWORD=drill", "POSTGRES_HOST_AUTH_METHOD=trust"):
        assert flag in start
    assert "--name sealedstack-drill-" in start and start.rstrip().endswith("postgres:16")
    assert any("pg_isready -h 127.0.0.1 -U postgres" in s for s in server.scripts())  # TCP: the init-phase server does not count
    assert any("createdb -U postgres drill" in s for s in server.scripts())


def test_the_limits_come_from_the_settings():
    db = make(stack_drill={"limits": {"memory": "512m", "cpus": "0.5"}})
    _, server, _ = run(db)
    assert "--memory 512m" in started(server)[0] and "--cpus 0.5" in started(server)[0]


def test_sample_loads_the_first_rows_of_every_table_with_constraints_off():
    db = make(app_drill={"sample_rows": 3})
    outcome, server, _ = run(db, level="sample")
    awk = next(stdin for _s, stdin, _t in server.calls if stdin)
    assert b"session_replication_role = replica" in awk and b'$0 == "\\\\."' in awk
    pipeline = next(c for c in db.consumers() if "awk -v n=3" in c)
    assert "pg_restore --data-only" in pipeline and "ON_ERROR_STOP=1" in pipeline
    assert any("--schema-only" in c for c in db.consumers())  # includes the schema level
    assert any("constraints and triggers off" in t for t in outcome.tested)


def test_tables_restores_the_named_tables_and_counts_them():
    db = make(app_drill={"tables": ["notes"]})
    outcome, _, _ = run(db, level="tables")
    data = next(c for c in db.consumers() if "--data-only" in c)
    assert "-t notes" in data and "PGOPTIONS='-c session_replication_role=replica'" in data
    assert any("notes (2 rows" in t for t in outcome.tested)
    assert db.psqls == ["SELECT pg_database_size(current_database())"]  # the live size for the disk rule


def test_tables_without_named_tables_says_so():
    outcome, _, _ = run(make(), level="tables")
    assert outcome.outcome == "passed" and any("no tables named" in t for t in outcome.tested)


def test_too_few_rows_fail_the_drill_and_remove_the_container():
    db = make(app_drill={"tables": ["notes"], "min_rows": 5})
    outcome, server, _ = run(db, server=FakeHost(rows="2"), level="tables")
    assert outcome.outcome == "failed" and "fewer than min_rows 5" in outcome.reason
    assert any("docker rm -fv" in s for s in server.scripts())


def test_full_restores_everything_and_checks_the_named_tables():
    db = make(app_drill={"tables": ["notes"]})
    outcome, _, _ = run(db, level="full")
    full = next(c for c in db.consumers() if "pg_restore -U postgres -d drill --no-owner" in c)
    assert "--schema-only" not in full and "--data-only" not in full
    assert outcome.outcome == "passed" and outcome.not_tested == ""
    assert any("constraints and triggers included" in t for t in outcome.tested)


def test_a_failing_restore_reports_the_first_lines_of_stderr_and_still_removes_the_container():
    db = make(fail={"--schema-only": (1, b"", b'ERROR: extension "postgis" is not available\nline 2')})
    outcome, server, _ = run(db)
    assert outcome.outcome == "failed" and "postgis" in outcome.reason and "line 2" in outcome.reason
    assert any("docker stop sealedstack-drill-" in s and "docker rm -fv" in s for s in server.scripts())


# -- places -----------------------------------------------------------------------

def test_the_server_place_never_hands_backup_bytes_to_the_engine():
    db = make(app_drill={"tables": ["notes"]})
    outcome, server, runner = run(db, level="tables")
    assert not hasattr(db, "s3_download") and runner.calls == []
    # every step that reads the backup is a stream_to on the server
    assert len(db.streams) >= 4 and {s["server"] for s in db.streams} == {"web-1"} and {s["key"] for s in db.streams} == {KEY}
    # the only stdin the engine ever sends is the sample script, never the dump
    assert all(stdin is None for _s, stdin, _t in server.calls)


def test_the_runner_place_sends_only_the_ddl_to_the_runner():
    db = make()
    outcome, server, runner = run(db, where="runner")
    assert outcome.outcome == "passed" and outcome.where == "runner"
    assert started(server) == []  # the container is on the runner
    ddl = next(c for c in db.consumers() if "-f -" in c)
    assert ddl == "docker run --rm -i --network none postgres:16 pg_restore --schema-only --no-owner --no-acl -f -"
    apply = [(s, stdin) for s, stdin, _t in runner.calls if "psql -U postgres -d drill -v ON_ERROR_STOP=1 -q" in s]
    assert apply and apply[0][1] == DDL
    assert [stdin for _s, stdin, _t in runner.calls if stdin] == [DDL]  # nothing else, and no rows, reaches the runner
    assert any("--label sealedstack.drill=1" in s for s in runner.scripts())
    assert any("the structure applies" in t and "no rows" in t for t in outcome.tested)
    assert "runner" in outcome.not_tested


def test_the_runner_place_verifies_on_the_server_and_checks_both_hosts():
    db = make()
    _, server, runner = run(db, where="runner")
    assert db.streams[0]["consumer"] == "cat > /dev/null" and "pg_restore --list" in db.streams[1]["consumer"]
    assert sum("docker info" in s for s in server.scripts()) == 1 and sum("docker info" in s for s in runner.scripts()) == 1


def test_the_runner_place_with_verify_only_touches_the_server():
    _, server, runner = run(make(), level="verify", where="runner")
    assert runner.calls == []


@pytest.mark.parametrize("level", ["sample", "tables", "full"])
def test_the_runner_place_refuses_data_levels(level):
    with pytest.raises(drill.DrillError, match="structure only"):
        run(make(), level=level, where="runner")


def test_the_server_and_runner_hosts_run_scripts_with_pipefail():
    db = make()
    host = drill.ServerHost(db, "web-1")
    host.run("echo 'a b'", b"in", 30)
    server, cmd, stdin = db.ssh_calls[0]
    assert server == "web-1" and stdin == b"in"
    assert cmd == "timeout 30 bash -o pipefail -c 'echo '\"'\"'a b'\"'\"''"
    assert drill.RunnerHost().run("false | true; echo $0 ok", None, 5)[0] == 0
    assert drill.RunnerHost().run("false | true", None, 5)[0] == 1  # pipefail
    assert drill.RunnerHost().run("sleep 5", None, 1)[0] == drill.RC_TIMEOUT


# -- the resource preflight ---------------------------------------------------------

@pytest.mark.parametrize("info,words", [
    ({"docker": "down"}, "docker does not answer"),
    ({"busy": 1}, "another drill"),
    ({"image": "missing"}, "not on the server"),
    ({"mem_avail": 1024**3}, "not enough free memory"),
    ({"load1": 3.0, "cores": 4}, "load"),
    ({"disk_free": 1000}, "not enough free disk"),
])
def test_each_preflight_rule_skips_and_starts_nothing(info, words):
    db = make()
    outcome, server, _ = run(db, server=FakeHost(info=probe(**info)))
    assert outcome.outcome == "skipped" and words in outcome.reason
    assert started(server) == [] and db.streams == [] and drill.exit_code(outcome) == 0


def test_the_image_may_be_pulled_when_allowed():
    db = make(stack_drill={"limits": {"allow_image_pull": True}})
    outcome, _, _ = run(db, server=FakeHost(info=probe(image="missing")))
    assert outcome.outcome == "passed"


def test_memory_must_be_twice_the_limit():
    ok, _, _ = run(make(stack_drill={"limits": {"memory": "1g"}}), server=FakeHost(info=probe(mem_avail=2 * 1024**3)))
    low, _, _ = run(make(stack_drill={"limits": {"memory": "1g"}}), server=FakeHost(info=probe(mem_avail=2 * 1024**3 - 1)))
    assert ok.outcome == "passed" and low.outcome == "skipped"


def test_the_load_limit_comes_from_the_settings():
    info = probe(load1=2.0, cores=4)
    assert run(make(), server=FakeHost(info=info))[0].outcome == "passed"
    assert run(make(stack_drill={"limits": {"max_load": 0.4}}), server=FakeHost(info=info))[0].outcome == "skipped"


def test_verify_needs_neither_memory_nor_disk():
    outcome, _, _ = run(make(), server=FakeHost(info=probe(mem_avail=1, disk_free=1)), level="verify")
    assert outcome.outcome == "passed"


def test_the_data_levels_need_the_ratio_times_the_live_size_free():
    info = probe(disk_free=1_100_000)
    low, server, _ = run(make(live=1_000_000), server=FakeHost(info=info), level="full")
    assert low.outcome == "skipped" and "not enough free disk" in low.reason and started(server) == []
    ok, _, _ = run(make(live=900_000), server=FakeHost(info=probe(disk_free=1_100_000)), level="full")
    assert ok.outcome == "passed"  # 1.2 x 900000 = 1080000
    ratio, _, _ = run(make(live=900_000, stack_drill={"min_free_ratio": 2}), server=FakeHost(info=probe(disk_free=1_100_000)), level="tables")
    assert ratio.outcome == "skipped"


def test_schema_and_sample_need_one_gib():
    assert run(make(), server=FakeHost(info=probe(disk_free=1024**3 - 1)))[0].outcome == "skipped"
    assert run(make(), server=FakeHost(info=probe(disk_free=1024**3)))[0].outcome == "passed"


def test_the_runner_is_measured_for_memory_disk_and_docker_but_not_load_or_image():
    info = probe(load1=50, cores=1, image="missing")
    assert run(make(), runner=FakeHost(info=info), where="runner")[0].outcome == "passed"
    for bad in ({"docker": "down"}, {"mem_avail": 1}, {"disk_free": 1}, {"busy": 2}):
        outcome, server, runner = run(make(), runner=FakeHost(info=probe(**bad)), where="runner")
        assert outcome.outcome == "skipped" and "on the runner" in outcome.reason and started(runner) == []


def test_the_server_is_still_measured_when_the_runner_hosts_the_container():
    outcome, _, runner = run(make(), server=FakeHost(info=probe(docker="down")), where="runner")
    assert outcome.outcome == "skipped" and "on the server" in outcome.reason and runner.calls == []


def test_the_preflight_script_is_filled_by_placeholders_and_sweeps_labelled_containers():
    db = make(stack_drill={"limits": {"timeout": "10m"}})
    _, server, _ = run(db)
    script = server.calls[0][0]
    assert "__" not in script and "postgres:16" in script and "-gt 600" in script
    for needle in ("{{.DockerRootDir}}", "nproc", "/proc/loadavg", "MemAvailable", "df -P -B1", "docker image inspect",
                   "label=sealedstack.drill=1", "docker rm -fv", "busy=", "removed="):
        assert needle in script


def test_the_sweep_removes_old_containers_and_counts_young_ones(tmp_path):
    now = 2_000_000_000
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  info) echo /nonexistent-docker-root ;;\n'
        '  image) exit 0 ;;\n'
        f'  ps) echo "aaa=sealedstack-drill-{now - 5000}"; echo "bbb=sealedstack-drill-{now - 10}"; echo "ccc=sealedstack-drill-{now - 9000}" ;;\n'
        f'  rm) echo "$@" >> {tmp_path}/removed ;;\n'
        "esac\n"
    )
    fake.chmod(0o755)
    date = tmp_path / "date"
    date.write_text(f"#!/bin/sh\necho {now}\n")
    date.chmod(0o755)
    script = drill.PREFLIGHT.replace("__IMAGE__", "postgres:16").replace("__TIMEOUT__", "1800")
    proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"})
    info = drill._probe(proc.stdout.encode())
    assert info["docker"] == "ok" and info["image"] == "present" and info["busy"] == "1" and info["removed"] == "2"
    assert (tmp_path / "removed").read_text().split() == ["rm", "-fv", "aaa", "rm", "-fv", "ccc"]


def test_removed_leftovers_are_a_warning():
    outcome, _, _ = run(make(), server=FakeHost(info=probe(removed=2)))
    assert outcome.outcome == "passed" and any("removed 2 leftover" in w for w in outcome.warnings)


# -- the deadline ---------------------------------------------------------------------

def test_every_step_gets_the_remaining_time_as_its_timeout():
    clock = Clock()
    db = make(clock=clock, step=10, stack_drill={"limits": {"timeout": "20m"}})
    server = FakeHost(clock=clock, step=10)
    drill_for(db, server, clock=clock).run("shop", level="tables")
    timeouts = [t for _s, _i, t in server.calls if t and t != 60] + [s["timeout_s"] for s in db.streams]
    assert timeouts and all(0 < t <= 1200 for t in timeouts)
    assert all(s["timeout_s"] > 0 for s in db.streams) and db.streams[0]["timeout_s"] < 1200
    ordered = sorted(timeouts, reverse=True)
    assert ordered[0] > ordered[-1]  # it shrinks as the drill goes on


def test_exceeding_the_deadline_fails_the_drill_and_still_removes_the_container():
    clock = Clock()
    db = make(clock=clock, step=40, stack_drill={"limits": {"timeout": "90s"}})
    server = FakeHost(clock=clock, step=40)
    outcome = drill_for(db, server, clock=clock).run("shop", level="schema")
    assert outcome.outcome == "failed" and "deadline" in outcome.reason
    assert not any("docker run -d" in s for s in server.scripts()) or any("docker rm -fv" in s for s in server.scripts())


def test_a_step_that_times_out_remotely_fails_the_drill_and_removes_the_container():
    db = make(fail={"--schema-only": (drill.RC_TIMEOUT, b"", b"")})
    outcome, server, _ = run(db)
    assert outcome.outcome == "failed" and "deadline" in outcome.reason
    assert any("docker rm -fv" in s for s in server.scripts())


def test_the_container_is_removed_on_success():
    _, server, _ = run(make())
    assert "docker stop sealedstack-drill-" in server.scripts()[-1] and "docker rm -fv" in server.scripts()[-1]


# -- the outcome and the report --------------------------------------------------------

KEYS = ["app", "time", "level", "where", "outcome", "backup", "backup_age_hours", "reason", "tested", "not_tested",
        "warnings", "duration_s"]


def test_the_outcome_dict_has_exactly_the_agreed_keys():
    outcome, _, _ = run(make(stamp=fresh_stamp(3)))
    d = outcome.to_dict()
    assert sorted(d) == sorted(KEYS)
    assert d["app"] == "shop" and d["time"].endswith("Z") and datetime.strptime(d["time"], "%Y-%m-%dT%H:%M:%SZ")
    assert d["backup"] == KEY and 2.9 <= d["backup_age_hours"] <= 3.2 and isinstance(d["tested"], list) and isinstance(d["warnings"], list)
    assert d["outcome"] == "passed" and isinstance(d["duration_s"], float)


def test_the_age_is_none_without_a_stamp():
    outcome, _, _ = run(make(stamp=""))
    # an empty stamp from the helper means the age is unknown
    assert outcome.backup_age_hours is None


def test_no_backup_is_a_failed_outcome_not_an_exception():
    outcome, server, _ = run(make(key=""))
    assert outcome.outcome == "failed" and "no backups of 'shop'" in outcome.reason and server.calls == []
    assert drill.exit_code(outcome) == 1


def test_a_stale_backup_gets_a_warning_and_a_fresh_one_does_not():
    stale, _, _ = run(make(stamp=fresh_stamp(60)))
    assert stale.outcome == "passed" and any("60.0 hours old" in w for w in stale.warnings)
    assert run(make(stamp=fresh_stamp(10)))[0].warnings == []
    limit, _, _ = run(make(stamp=fresh_stamp(10), stack_drill={"max_backup_age": "6h"}))
    assert len(limit.warnings) == 1


def test_the_outcome_is_recorded(monkeypatch):
    from sealedstack import drillrecord

    seen = []
    monkeypatch.setattr(drillrecord, "record", lambda config, env, d: seen.append(d))
    outcome, _, _ = run(make())
    assert seen == [outcome.to_dict()]


def test_a_failing_recorder_never_breaks_the_drill(monkeypatch):
    from sealedstack import drillrecord

    def boom(*a, **k):
        raise RuntimeError("bucket down")

    monkeypatch.setattr(drillrecord, "record", boom)
    assert run(make())[0].outcome == "passed"


def test_exit_codes():
    assert drill.exit_code(drill.Outcome("a", "schema", "server", outcome="passed")) == 0
    assert drill.exit_code(drill.Outcome("a", "schema", "server", outcome="skipped")) == 0
    assert drill.exit_code(drill.Outcome("a", "schema", "server", outcome="failed")) == 1


def report(outcome, gha=False):
    out = io.StringIO()
    drill.print_report(outcome, out, gha)
    return out.getvalue()


def test_the_report_lists_what_was_tested_and_what_was_not():
    outcome, _, _ = run(make())
    text = report(outcome)
    assert "tested:" in text and "not tested:" in text and "level schema" in text and KEY in text
    assert "that any data loads" in text and "::" not in text


def test_the_annotations_appear_only_in_github_actions():
    skipped, _, _ = run(make(), server=FakeHost(info=probe(docker="down")))
    assert "::warning title=backup check skipped::shop: docker does not answer" in report(skipped, True)
    assert "::" not in report(skipped, False)
    failed, _, _ = run(make(fail={"--schema-only": (1, b"", b"boom\nsecond")}))
    text = report(failed, True)
    assert "::error title=backup check failed::shop:" in text and "boom | second" in text
    assert "::" not in report(run(make())[0], True)


def test_an_annotation_never_breaks_across_lines():
    outcome = drill.Outcome("a", "schema", "server", outcome="failed", reason="line one\nline two 100%")
    text = report(outcome, True)
    assert "::error title=backup check failed::a: line one%0Aline two 100%25" in text


# -- settings ---------------------------------------------------------------------------

def test_settings_defaults():
    s = drill.settings_for({}, {})
    assert (s.level, s.where, s.tables, s.min_rows, s.sample_rows) == ("schema", "server", [], 1, 1)
    assert (s.min_free_ratio, s.memory, s.cpus, s.timeout, s.max_load, s.allow_image_pull, s.max_backup_age) == (
        1.2, "1g", "1", 1800, 0.7, False, 48 * 3600)


def test_settings_precedence_argument_over_app_over_stack():
    stack = {"drill": {"level": "verify", "where": "server", "min_free_ratio": 2, "limits": {"memory": "2g", "timeout": "1h"}}}
    app = {"drill": {"level": "tables", "where": "server", "tables": ["a"], "min_rows": 4, "sample_rows": 7}}
    s = drill.settings_for(stack, app)
    assert (s.level, s.tables, s.min_rows, s.sample_rows, s.min_free_ratio, s.memory, s.timeout) == ("tables", ["a"], 4, 7, 2.0, "2g", 3600)
    assert drill.settings_for(stack, {}).level == "verify"
    assert drill.settings_for(stack, app, level="full", tables=["b"]).level == "full"
    assert drill.settings_for(stack, app, level="full", tables=["b"]).tables == ["b"]
    assert drill.settings_for({"drill": {"where": "server"}}, {"drill": {"where": "runner", "level": "verify"}}).where == "runner"
    assert drill.settings_for({"drill": {"where": "server"}}, {"drill": {"where": "server"}}, where="runner", level="schema").where == "runner"


def test_settings_validation():
    with pytest.raises(drill.DrillError, match="unknown drill level"):
        drill.settings_for({}, {}, level="deep")
    with pytest.raises(drill.DrillError, match="unknown drill place"):
        drill.settings_for({}, {}, where="moon")
    with pytest.raises(drill.DrillError, match="not a table name"):
        drill.settings_for({}, {}, tables=["notes; DROP TABLE x"])
    with pytest.raises(drill.DrillError, match="structure only"):
        drill.settings_for({"drill": {"where": "runner", "level": "full"}}, {})
    with pytest.raises(drill.DrillError, match="structure only"):
        drill.settings_for({}, {"drill": {"where": "runner", "level": "sample"}})
    with pytest.raises(drill.DrillError, match="not a duration"):
        drill.settings_for({"drill": {"limits": {"timeout": "soon"}}}, {})
    with pytest.raises(drill.DrillError, match="not a size"):
        drill.settings_for({"drill": {"limits": {"memory": "lots"}}}, {})
    with pytest.raises(drill.DrillError, match="number of CPUs"):
        drill.settings_for({"drill": {"limits": {"cpus": "1; rm"}}}, {})
    assert drill.settings_for({"drill": {"where": "runner"}}, {}).level == "schema"  # the default fits the runner


def test_durations_and_sizes():
    assert [drill.parse_duration(v) for v in ("90s", "30m", "2h", "48h", "1d")] == [90, 1800, 7200, 172800, 86400]
    for bad in ("", "30", "m", "1.5h", "-5m"):
        with pytest.raises(drill.DrillError):
            drill.parse_duration(bad)
    assert [drill.parse_size(v) for v in ("512m", "1g", "100", "64k", "1gb")] == [512 * 1024**2, 1024**3, 100, 65536, 1024**3]


def test_the_archive_contents_are_summarised():
    assert drill.parse_toc(TOC) == {"entries": 3, "data_tables": ["public.notes", "public.tags"]}


def test_levels_are_ordered():
    assert drill.LEVELS == ("verify", "schema", "sample", "tables", "full")


# -- the schemas ---------------------------------------------------------------------------

def schema_errors(doc, name):
    return validate_mod.schema_errors(doc, REPO / "schemas" / f"{name}.schema.json")


def stack_doc(drill_block):
    import yaml

    from sealedstack import scaffold

    stack = scaffold.stack_config(infra_name="my-infra", domain="example.com", owner_email="you@example.com",
                                  account_id="acct123", tailnet="tail1234.ts.net")
    stack["drill"] = drill_block
    return yaml.safe_load(yaml.safe_dump(stack))


def test_the_stack_schema_accepts_the_new_drill_settings():
    block = {"scheduled": False, "level": "schema", "where": "runner", "min_free_ratio": 1.2, "max_backup_age": "48h",
             "limits": {"memory": "1g", "cpus": "1", "timeout": "30m", "max_load": 0.7, "allow_image_pull": False}}
    assert schema_errors(stack_doc(block), "stack") == []
    assert schema_errors(stack_doc({"limits": {"cpus": 0.5}}), "stack") == []


@pytest.mark.parametrize("block", [
    {"where": "moon"},
    {"max_backup_age": "soon"},
    {"limits": {"timeout": "30"}},
    {"limits": {"bogus": 1}},
    {"memory": "1g"},  # moved to limits.memory
])
def test_the_stack_schema_rejects_bad_drill_settings(block):
    assert schema_errors(stack_doc(block), "stack") != []


def test_the_schema_default_level_is_schema():
    import json

    schema = json.loads((REPO / "schemas" / "stack.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["drill"]["properties"]["level"]["default"] == "schema"


def apps_doc(drill_block):
    return {"apps": {"shop": {"source": {"image": "ghcr.io/me/shop:1.0"}, "servers": ["web-1"], "hostnames": ["shop.example.com"],
                              "access": "public", "drill": drill_block}}}


def test_the_apps_schema_accepts_where_in_the_app_drill_block():
    ok = apps_doc({"enabled": True, "level": "schema", "where": "runner", "tables": [], "min_rows": 1, "sample_rows": 1})
    errors = schema_errors(ok, "apps")
    assert not [e for e in errors if "drill" in e], errors
    bad = schema_errors(apps_doc({"where": "moon"}), "apps")
    assert any("drill" in e or "where" in e for e in bad)
