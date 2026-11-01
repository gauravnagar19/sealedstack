"""D-085: a restore streams the backup from R2 to the server; no backup byte passes through the CI runner."""
from __future__ import annotations

import gzip
import io
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from sealedstack import config as config_mod, db as dbmod
from test_engine_db import LISTING, FakeTransport, make_db

ENDPOINT = "https://acct.r2.cloudflarestorage.com"
KEY = "demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T18-00-00-056Z.sql.gz"


class StreamTransport(FakeTransport):
    """FakeTransport with R2 credentials and ``ssh_full``; records every ssh call (``ssh`` and ``ssh_full``)."""

    def __init__(self, psql_value="0", stream_rc=0, stream_err=b""):
        super().__init__(psql_value)
        self.endpoint, self.access_key, self.secret_key = ENDPOINT, "AKTESTKEY", "SKTESTSECRET"
        self.stream_rc, self.stream_err = stream_rc, stream_err

    def ssh_full(self, server, remote_cmd, stdin=None):
        self.calls.append((server, remote_cmd, stdin))
        if "curl" in remote_cmd:
            return self.stream_rc, b"", self.stream_err
        rc, out = self.ssh(server, remote_cmd, stdin)
        self.calls.pop()  # ssh() recorded it a second time
        return rc, out, b""


def streaming_db(tmp_path, transport=None, deployments=None):
    db = make_db(tmp_path, deployments=deployments)
    db.t = transport or StreamTransport()
    return db


def ssh_commands(db):
    return [call for call in db.t.calls if call[0] != "aws"]


def stream_calls(db):
    return [call for call in ssh_commands(db) if "curl" in call[1]]


# -- presigned ------------------------------------------------------------------
def test_the_presigned_url_is_built_from_the_transports_r2_credentials(tmp_path):
    db = streaming_db(tmp_path)
    url = db.presigned(KEY, expires=600)
    assert url.startswith(f"{ENDPOINT}/{db.bucket}/{KEY}?X-Amz-Algorithm=AWS4-HMAC-SHA256")
    assert "X-Amz-Credential=AKTESTKEY%2F" in url and "X-Amz-Expires=600" in url
    assert "SKTESTSECRET" not in url
    assert "X-Amz-Expires=900" in db.presigned(KEY)


def test_presigning_without_an_endpoint_is_a_clear_error(tmp_path):
    db = make_db(tmp_path)  # the plain FakeTransport has no endpoint
    with pytest.raises(dbmod.DbError, match="R2 endpoint"):
        db.presigned(KEY)


# -- stream_to ------------------------------------------------------------------
def test_stream_to_sends_the_url_on_stdin_and_never_in_argv(tmp_path):
    db = streaming_db(tmp_path)
    rc, _out, _err = db.stream_to("web-1", "pg_restore --list", KEY)
    assert rc == 0
    [(server, remote, stdin)] = stream_calls(db)
    assert server == "web-1"
    url = stdin.decode().rstrip("\n")
    assert url.startswith(f"{ENDPOINT}/") and stdin.endswith(b"\n") and stdin.count(b"\n") == 1
    assert "X-Amz-Signature" not in remote and ENDPOINT not in remote
    argv = shlex.split(remote)
    assert argv[:4] == ["bash", "-o", "pipefail", "-c"]
    assert argv[4] == 'IFS= read -r u; curl -fsS --retry 2 --connect-timeout 20 "$u" | gunzip | pg_restore --list'


def test_each_stream_presigns_a_fresh_url(tmp_path, monkeypatch):
    db = streaming_db(tmp_path)
    urls = iter(["https://x/1", "https://x/2"])
    monkeypatch.setattr(db, "presigned", lambda key, expires=900: next(urls))
    db.stream_to("web-1", "cat", KEY)
    db.stream_to("web-1", "cat", KEY)
    assert [call[2] for call in stream_calls(db)] == [b"https://x/1\n", b"https://x/2\n"]


def test_stream_to_drops_pipefail_when_asked_and_adds_a_timeout(tmp_path):
    db = streaming_db(tmp_path)
    db.stream_to("web-1", "pg_restore --list", KEY, pipefail=False)
    db.stream_to("web-1", "cat", KEY, timeout_s=600)
    no_pipefail, timed = (shlex.split(call[1]) for call in stream_calls(db))
    assert no_pipefail[:2] == ["bash", "-c"] and "pipefail" not in no_pipefail
    assert timed[:6] == ["timeout", "600s", "bash", "-o", "pipefail", "-c"]


def test_stream_to_falls_back_to_plain_ssh_with_empty_stderr(tmp_path):
    db = make_db(tmp_path)  # FakeTransport: ssh and aws only
    db.t.endpoint, db.t.access_key, db.t.secret_key = ENDPOINT, "AK", "SK"
    assert db.stream_to("web-1", "pg_restore -d x", KEY) == (0, b"", b"")
    remote, stdin = db.t.calls[-1][1], db.t.calls[-1][2]
    assert "curl" in remote and stdin.startswith(ENDPOINT.encode()) and b"X-Amz-Signature" not in remote.encode()


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("gunzip")), reason="needs bash and gunzip")
def test_the_remote_command_really_reads_the_url_from_stdin_and_unpacks_into_the_consumer(tmp_path):
    """Run the exact remote command locally with a stub curl that serves one gzip object for one URL."""
    url = "https://acct.r2.cloudflarestorage.com/b/k.sql.gz?X-Amz-Signature=abc&X-Amz-Expires=900"
    (tmp_path / "obj.gz").write_bytes(gzip.compress(b"PGDMP payload\n"))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "curl").write_text(
        "#!/bin/sh\n"
        f'for a in "$@"; do last="$a"; done\n[ "$last" = "{url}" ] || {{ echo "bad url" >&2; exit 22; }}\n'
        f'cat "{tmp_path}/obj.gz"\n'
    )
    (bindir / "curl").chmod(0o755)
    out = tmp_path / "restored"
    db = make_db(tmp_path)
    captured = {}

    def ssh(server, remote_cmd, stdin=None):
        captured["remote"] = remote_cmd
        proc = subprocess.run(["bash", "-c", remote_cmd], input=stdin, capture_output=True,
                              env={**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"})
        return proc.returncode, proc.stdout

    db.t.ssh = ssh
    db.presigned = lambda key, expires=900: url
    rc, _o, _e = db.stream_to("web-1", f"cat > {shlex.quote(str(out))}", "k.sql.gz")
    assert rc == 0 and out.read_bytes() == b"PGDMP payload\n"
    assert url not in captured["remote"]
    db.presigned = lambda key, expires=900: url + "x"  # curl fails: pipefail makes the whole pipeline fail
    rc, _o, _e = db.stream_to("web-1", "cat > /dev/null", "k.sql.gz")
    assert rc != 0
    rc, _o, _e = db.stream_to("web-1", "cat > /dev/null", "k.sql.gz", pipefail=False)
    assert rc == 0


# -- restore --------------------------------------------------------------------
def test_restore_streams_the_backup_to_the_server_instead_of_downloading_it(tmp_path):
    db = streaming_db(tmp_path, deployments={"compose-1": [{"id": 1}]})
    assert db.restore("shop", KEY) == 0
    assert not any(call[0] == "aws" and call[1][:2] == ["s3", "cp"] for call in db.t.calls)
    [(server, remote, stdin)] = stream_calls(db)
    assert server == "web-1" and stdin.startswith(f"{ENDPOINT}/{db.bucket}/".encode())
    script = shlex.split(remote)[-1]
    assert script.endswith(
        "| gunzip | docker run --rm -i --network dokploy-network --env-file '/tmp/sst/env' postgres:16 "
        "pg_restore --no-owner --no-acl --clean --if-exists -d 'shop'"
    )
    assert "X-Amz-Signature" not in remote and "X-Amz-Signature" not in db.out.getvalue()
    # the env file is removed after the stream, and the app is stopped before and started after
    assert any("rm -rf -- '/tmp/sst'" in call[1] for call in ssh_commands(db)[ssh_commands(db).index(stream_calls(db)[0]):])
    assert [path for path, _ in db.client.posts] == ["/api/compose.stop", "/api/compose.start"]


def test_restore_still_refuses_a_non_empty_database_without_force(tmp_path):
    db = streaming_db(tmp_path, transport=StreamTransport(psql_value="3"))
    with pytest.raises(dbmod.DbError, match="not empty"):
        db.restore("shop", KEY)
    assert stream_calls(db) == []
    assert db.restore("shop", KEY, force=True) == 0 and len(stream_calls(db)) == 1


def test_restore_uses_the_newest_backup_when_no_key_is_given(tmp_path):
    db = streaming_db(tmp_path)
    db._names = {"shop": "demoapp"}
    db._urls = {"shop": "postgres://u:p@db:5432/demoapp"}
    listing = b"2026-11-01 18:00:01   397 shop-db-x/shop/demoapp/2026-11-01T18-00-00-056Z.sql.gz\n"
    db.t.aws = lambda args, stdin=None: (0, listing)
    assert db.restore("shop") == 0
    assert stream_calls(db)[0][2].startswith(f"{ENDPOINT}/{db.bucket}/shop-db-x/shop/demoapp/".encode())


def test_a_failed_stream_raises_with_the_first_lines_of_stderr_and_restarts_the_app(tmp_path):
    err = b"curl: (22) The requested URL returned error: 403\n" + b"".join(b"line %d\n" % i for i in range(10))
    db = streaming_db(tmp_path, transport=StreamTransport(stream_rc=1, stream_err=err),
                      deployments={"compose-1": [{"id": 1}]})
    with pytest.raises(dbmod.DbError) as exc:
        db.restore("shop", KEY)
    assert "pg_restore failed for shop" in str(exc.value) and "error: 403" in str(exc.value)
    assert "line 3" in str(exc.value) and "line 9" not in str(exc.value)
    assert [path for path, _ in db.client.posts] == ["/api/compose.stop", "/api/compose.start"]


# -- listing ----------------------------------------------------------------------
def test_latest_backup_entry_returns_key_size_and_utc_stamp(tmp_path):
    db = make_db(tmp_path)
    db._names = {"demo-app": "demoapp"}
    db.t.aws = lambda args, stdin=None: (0, LISTING)
    assert db.latest_backup_entry("demo-app") == (
        "demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz", 1058, "2026-11-01 08:53:45")
    db.t.aws = lambda args, stdin=None: (1, b"")
    assert db.latest_backup_entry("demo-app") == ("", 0, "")


def _listing_db(tmp_path):
    db = make_db(tmp_path)
    urls = {"demo-app": "postgres://a", "other": "postgres://b", "fresh": "postgres://c"}
    db._output = lambda name: urls if name == "database_urls" else {}
    db._names = {"demo-app": "demoapp", "other": "otherdb", "fresh": "fresh"}
    listings = []

    def aws(args, stdin=None):
        listings.append(args)
        return 0, LISTING

    db.t.aws = aws
    return db, listings


def test_list_backups_gives_every_managed_database_with_its_newest_backup_and_age(tmp_path):
    db, listings = _listing_db(tmp_path)
    rows = db.list_backups(now=datetime(2026, 11, 1, 18, 53, 45, tzinfo=timezone.utc))
    assert len(listings) == 1  # one bucket listing for all apps
    assert rows == [
        {"app": "demo-app", "key": "demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz",
         "size": 1058, "stamp": "2026-11-01 08:53:45", "age_hours": 10.0},
        {"app": "fresh", "key": "", "size": 0, "stamp": "", "age_hours": None},
        {"app": "other", "key": "other-db-abc/other/otherdb/2026-11-01T07-00-00-000Z.sql.gz",
         "size": 90, "stamp": "2026-11-01 07:00:00", "age_hours": 11.9},
    ]


def test_list_backups_lists_apps_that_turned_the_drill_off(tmp_path):
    db, _ = _listing_db(tmp_path)
    db.config.apps["apps"] = {"other": {"drill": {"enabled": False}}}
    assert "other" in [row["app"] for row in db.list_backups()]


def test_format_backup_rows():
    rows = [
        {"app": "a", "key": "x/a/a/1.sql.gz", "size": 3 * 1024 * 1024 + 100, "stamp": "2026-11-01 00:00:00", "age_hours": 26.5},
        {"app": "b", "key": "", "size": 0, "stamp": "", "age_hours": None},
        {"app": "c", "key": "x/c/c/1.sql.gz", "size": 397, "stamp": "2026-11-01 00:00:00", "age_hours": 0.0},
    ]
    assert dbmod.format_backup_rows(rows) == [
        "a: x/a/a/1.sql.gz (3.0 MiB, 26.5h old)",
        "b: no backup yet",
        "c: x/c/c/1.sql.gz (397 B, 0.0h old)",
    ]


# -- main -------------------------------------------------------------------------
class RecordingDb:
    def __init__(self, out):
        self.out = out
        self.calls = []

    def drill(self, app, table="", level="", where=""):
        self.calls.append(("drill", app, table, level, where))
        return 0

    def drill_all(self, table="", level="", where=""):
        self.calls.append(("drill_all", table, level, where))
        return 0


@pytest.fixture
def run_main(tmp_path, monkeypatch, capsys):
    cfg = make_db(tmp_path).config

    def run(argv, db):
        monkeypatch.setattr(config_mod, "load_config", lambda **kwargs: cfg)
        monkeypatch.setattr(dbmod, "Db", lambda config, env: db)
        return dbmod.main(argv)

    return run


@pytest.mark.parametrize("argv, expected", [
    (["drill", "shop"], ("drill", "shop", "", "", "")),
    (["drill", "shop", "notes", "--level", "schema", "--where", "runner"], ("drill", "shop", "notes", "schema", "runner")),
    (["drill", "--where=server", "shop", "--level=full"], ("drill", "shop", "", "full", "server")),
    (["drill", "all", "notes", "--where", "runner"], ("drill_all", "notes", "", "runner")),
    (["drill", "all", "--level=verify", "--where=server"], ("drill_all", "", "verify", "server")),
])
def test_drill_parses_level_and_where(run_main, argv, expected):
    db = RecordingDb(io.StringIO())
    assert run_main(argv, db) == 0
    assert db.calls == [expected]


def test_drill_rejects_an_unknown_where(run_main, capsys):
    db = RecordingDb(io.StringIO())
    assert run_main(["drill", "shop", "--where", "laptop"], db) == 1
    assert db.calls == [] and "--where must be one of server, runner" in capsys.readouterr().err


def test_db_list_prints_one_line_per_app(run_main, tmp_path):
    (tmp_path / "l").mkdir()
    db, _ = _listing_db(tmp_path / "l")
    assert run_main(["list"], db) == 0
    lines = db.out.getvalue().splitlines()
    assert lines[0].startswith("demo-app: demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz (1.0 KiB, ")
    assert lines[0].endswith("h old)")
    assert lines[1] == "fresh: no backup yet"
    assert lines[2].startswith("other: other-db-abc/") and len(lines) == 3


def test_db_list_with_no_managed_database(run_main, tmp_path):
    (tmp_path / "n").mkdir()
    db = make_db(tmp_path / "n")
    db._output = lambda name: {}
    assert run_main(["list"], db) == 0
    assert "no app has a managed database" in db.out.getvalue()


def test_db_drill_passes_where_through_to_the_drill(tmp_path, monkeypatch):
    from sealedstack import drill as drill_mod

    seen = {}

    class FakeDrill:
        def __init__(self, db, out=None, sleep=None):
            pass

        def run(self, app, **kwargs):
            seen.update(app=app, **kwargs)
            outcome = drill_mod.Outcome(app=app, level=kwargs["level"], where=kwargs["where"])
            outcome.outcome = "passed"
            return outcome

    monkeypatch.setattr(drill_mod, "Drill", FakeDrill)
    db = make_db(tmp_path)
    assert db.drill("shop", "notes", "schema", "runner") == 0
    assert seen == {"app": "shop", "level": "schema", "where": "runner", "tables": ["notes"]}
    db._output = lambda name: {"shop": "postgres://x"} if name == "database_urls" else {}
    assert db.drill_all("", "full", "server") == 0
    assert seen == {"app": "shop", "level": "full", "where": "server", "tables": []}


def test_db_drill_prints_the_report_and_turns_the_outcome_into_an_exit_code(tmp_path, monkeypatch):
    """Integration seam between db.py and drill.py v2: run() returns an Outcome, not an int (D-085)."""
    from sealedstack import drill as drill_mod

    db = streaming_db(tmp_path)
    seen = []

    def fake_run(self, app, *, level="", where="", tables=()):
        o = drill_mod.Outcome(app=app, level=level or "schema", where=where or "server")
        o.outcome = "failed" if app == "bad" else "passed"
        o.reason = "restore error" if app == "bad" else ""
        seen.append((app, level, where, list(tables)))
        return o

    monkeypatch.setattr(drill_mod.Drill, "run", fake_run)
    assert db.drill("good", "notes", "tables", "server") == 0
    assert db.drill("bad") == 1
    assert seen[0] == ("good", "tables", "server", ["notes"])
    assert "restore error" in db.out.getvalue()
