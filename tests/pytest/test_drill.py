"""D-084: the restore drill runs in an isolated container, in levels, and says what it did not test."""
from __future__ import annotations

import gzip
import io

import pytest

from sealedstack import db as dbmod, drill

from test_engine_db import FakeClient, FakeSource, FakeState, make_config

TOC = (
    ";\n; Archive created at 2026-11-01\n"
    "215; 1259 16385 TABLE public notes demo\n"
    "3260; 0 16385 TABLE DATA public notes demo\n"
    "3261; 0 16390 TABLE DATA public tags demo\n"
)
DUMP = gzip.compress(b"PGDMP" + b"x" * 40)
LISTING = f"2026-11-01 18:00:01        {len(DUMP)} old-db-abc/shop/shop/2026-11-01T18-00-00Z.sql.gz\n".encode()


class Box:
    """A scripted server: records every ssh command and answers by what it contains."""

    def __init__(self, fail=None, free=10**12, rows="2", live="1000"):
        self.calls, self.fail, self.free, self.rows, self.live = [], fail or {}, free, rows, live

    def ssh_full(self, server, cmd, stdin=None):
        self.calls.append((server, cmd, stdin))
        for needle, answer in self.fail.items():
            if needle in cmd:
                return answer
        if "mktemp -d" in cmd:  # the engine's remote env file for the live instance (only the full level's size query)
            return 0, b"/tmp/x\n", b""
        if "pg_restore --list" in cmd:
            return 0, TOC.encode(), b""
        if "df --output" in cmd:
            return 0, str(self.free).encode(), b""
        if "FROM pg_tables" in cmd:
            return 0, b"2", b""
        if "SELECT count(*) FROM notes" in cmd:
            return 0, self.rows.encode(), b""
        if "pg_database_size" in (stdin or b"").decode("utf-8", "ignore"):
            return 0, self.live.encode(), b""
        return 0, b"", b""

    def ssh(self, server, cmd, stdin=None):
        rc, out, _ = self.ssh_full(server, cmd, stdin)
        return rc, out

    def aws(self, args, stdin=None):
        return (0, LISTING) if "ls" in args else (0, DUMP)

    def commands(self):
        return [cmd for _server, cmd, _stdin in self.calls]


def make(tmp_path, box, *, app_drill=None, stack_drill=None):
    db = dbmod.Db(make_config(tmp_path), {}, source=FakeSource({}),
                  state=FakeState({"shop": "postgres://u:p@db:5432/shop"}, {"shop": "shop"}, {"shop": {"web-1": "c1"}}),
                  client=FakeClient(None), transport=box, out=io.StringIO())
    db.config.apps["apps"] = {"shop": {"servers": ["web-1"], **({"drill": app_drill} if app_drill else {})}}
    if stack_drill:
        db.config.stack["drill"] = stack_drill
    return db


def run(tmp_path, box, **kwargs):
    db = make(tmp_path, box, app_drill=kwargs.pop("app_drill", {"tables": ["notes"]}), stack_drill=kwargs.pop("stack_drill", None))
    rc = db.drill("shop", **kwargs)
    return rc, db.out.getvalue()


def test_the_default_level_restores_the_structure_and_the_named_tables_in_an_isolated_container(tmp_path):
    box = Box()
    rc, text = run(tmp_path, box)
    cmds = box.commands()
    start = next(c for c in cmds if "docker run -d" in c)
    assert rc == 0 and "--network none" in start and "--memory 1g" in start and "--rm" in start  # no network, capped, self-removing
    assert any("--schema-only" in c for c in cmds) and any("--data-only" in c and "-t notes" in c for c in cmds)
    assert "level tables" in text and "tested:" in text and "not tested:" in text and "notes (2 rows" in text
    assert any("docker stop" in c for c in cmds)  # the container is always cleaned up


def test_the_live_database_is_never_connected_to_for_the_default_levels(tmp_path):
    box = Box()
    run(tmp_path, box)
    assert not any("--env-file" in c for c in box.commands())  # the live instance is reached through an env file only


def test_verify_needs_no_database_and_no_disk(tmp_path):
    box = Box()
    rc, text = run(tmp_path, box, level="verify")
    assert rc == 0 and not any("docker run -d" in c for c in box.commands()) and not any("df --output" in c for c in box.commands())
    assert "archive contents" in text and "that the structure applies" in text


def test_schema_level_loads_no_rows(tmp_path):
    box = Box()
    rc, text = run(tmp_path, box, level="schema")
    assert rc == 0 and not any("--data-only" in c for c in box.commands()) and "the structure applies" in text


def test_sample_level_loads_the_first_rows_of_every_table_with_constraints_off(tmp_path):
    box = Box()
    rc, text = run(tmp_path, box, level="sample", app_drill={"sample_rows": 3})
    awk = next(stdin for _s, cmd, stdin in box.calls if "sample.awk'" in cmd and stdin)
    assert rc == 0 and b"session_replication_role = replica" in awk and b'$0 == "\\\\."' in awk
    pipeline = next(c for c in box.commands() if "awk -v n=3" in c)
    assert "pg_restore --data-only" in pipeline and "ON_ERROR_STOP=1" in pipeline and "constraints and triggers off" in text


def test_the_full_level_checks_the_disk_first_and_skips_instead_of_risking_it(tmp_path):
    box = Box(free=100, live="5000")
    with pytest.raises(dbmod.DbError, match="not enough free disk space"):
        run(tmp_path, box, level="full")
    assert not any("docker run -d" in c for c in box.commands())  # nothing was started


def test_the_full_level_restores_everything_when_there_is_room(tmp_path):
    box = Box(free=10**12)
    rc, text = run(tmp_path, box, level="full")
    assert rc == 0 and any("pg_restore -U postgres -d drill --no-owner" in c and "--schema-only" not in c for c in box.commands())
    assert "constraints and triggers included" in text and "not tested" not in text


def test_too_few_rows_after_the_restore_fail_the_drill(tmp_path):
    with pytest.raises(dbmod.DbError, match="fewer than min_rows 5"):
        run(tmp_path, Box(rows="2"), app_drill={"tables": ["notes"], "min_rows": 5})


def test_a_failing_step_names_the_reason_and_still_removes_the_container(tmp_path):
    box = Box(fail={"--schema-only": (1, b"", b'ERROR: extension "postgis" is not available')})
    with pytest.raises(dbmod.DbError, match="postgis"):
        run(tmp_path, box)
    assert any("docker stop" in c for c in box.commands())


def test_a_truncated_backup_is_caught_by_the_checksum(tmp_path):
    box = Box()
    box.aws = lambda args, stdin=None: (0, LISTING) if "ls" in args else (0, DUMP[:-10])
    with pytest.raises(dbmod.DbError, match="not a complete gzip"):
        run(tmp_path, box)


def test_settings_precedence_and_validation():
    s = drill.settings_for({"drill": {"level": "schema", "min_free_ratio": 2}}, {"drill": {"tables": ["a"], "min_rows": 4}})
    assert (s.level, s.tables, s.min_rows, s.min_free_ratio) == ("schema", ["a"], 4, 2.0)
    assert drill.settings_for({"drill": {"level": "schema"}}, {}, level="full").level == "full"
    with pytest.raises(drill.DrillError, match="unknown drill level"):
        drill.settings_for({}, {}, level="deep")
    with pytest.raises(drill.DrillError, match="not a table name"):
        drill.settings_for({}, {}, tables=["notes; DROP TABLE x"])


def test_the_archive_contents_are_summarised():
    assert drill.parse_toc(TOC) == {"entries": 3, "data_tables": ["public.notes", "public.tags"]}


def test_drill_all_skips_apps_that_turned_the_drill_off(tmp_path):
    db = make(tmp_path, Box(), app_drill={"enabled": False})
    db._output = lambda name: {"shop": "postgres://x"}
    assert db.drill_all() == 0 and "no app has a managed database to drill" in db.out.getvalue()
