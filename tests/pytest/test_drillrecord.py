"""The newest restore-drill result per app, in the state bucket, shown by `status` (D-085, R8/R9)."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import drillrecord, preflight, status, statusissue  # noqa: E402
from sealedstack.http import StubClient  # noqa: E402
from sealedstack.s3 import S3Client, S3Error  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

from fake_s3 import ACCESS_KEY, SECRET_KEY, FakeS3  # noqa: E402
from test_engine_preflight import make_config  # noqa: E402

KEY = "sealedstack/example-infra/backup-checks.json"
NOW = datetime(2026, 11, 2, 6, 0, 12, tzinfo=timezone.utc)


class MemoryS3:
    """The two calls drillrecord uses, in memory; ``fail_get``/``fail_put`` raise like a broken bucket."""

    def __init__(self, objects=None, *, fail_get=False, fail_put=False):
        self.objects = dict(objects or {})
        self.fail_get, self.fail_put = fail_get, fail_put
        self.puts = 0

    def get(self, key):
        if self.fail_get:
            raise S3Error(f"get {key} failed (HTTP 500)")
        return self.objects.get(key)

    def put(self, key, body):
        if self.fail_put:
            raise S3Error(f"put {key} failed (HTTP 500)")
        self.puts += 1
        self.objects[key] = body


def outcome(app="demo-app", result="passed", **extra):
    base = {
        "app": app, "time": "2026-11-02T03:00:12Z", "level": "schema", "where": "server", "outcome": result,
        "backup": f"{app}-db/{app}/2026-11-02T00-00-00-000Z.sql.gz", "backup_age_hours": 3.0, "reason": "",
        "tested": ["gzip checksum", "schema applies"], "not_tested": "row data", "warnings": [], "duration_s": 41.5,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- storage
def test_record_then_load_round_trips_and_keeps_other_apps(tmp_path):
    config, store = make_config(tmp_path), MemoryS3()
    assert drillrecord.record(config, {}, outcome("demo-app"), client=store) is True
    assert drillrecord.record(config, {}, outcome("blog", "failed", reason="pg_restore: extension postgis missing"), client=store)
    assert drillrecord.record(config, {}, outcome("demo-app", "skipped", reason="free memory 0.8 GiB < 2 GiB"), client=store)
    data = drillrecord.load(config, {}, client=store)
    assert set(data["apps"]) == {"demo-app", "blog"}
    assert data["apps"]["demo-app"]["outcome"] == "skipped"  # the newest result replaces the older one
    assert data["apps"]["blog"]["reason"].startswith("pg_restore")
    assert list(store.objects) == [KEY]  # one object, namespaced like the state lock
    assert drillrecord.record_key("example-infra") == KEY


def test_a_missing_object_loads_as_no_results(tmp_path):
    assert drillrecord.load(make_config(tmp_path), {}, client=MemoryS3()) == {"apps": {}}
    assert drillrecord.load_for_status(make_config(tmp_path), {}, client=MemoryS3()) == {"apps": {}}


def test_an_unreadable_object_starts_a_new_record(tmp_path, capsys):
    store = MemoryS3({KEY: b"not json"})
    assert drillrecord.load(make_config(tmp_path), {}, client=store) == {"apps": {}}
    assert drillrecord.record(make_config(tmp_path), {}, outcome(), client=store)
    assert set(json.loads(store.objects[KEY])["apps"]) == {"demo-app"}
    assert "not valid JSON" in capsys.readouterr().err


def test_storage_errors_never_raise(tmp_path, capsys):
    config = make_config(tmp_path)
    broken_read = MemoryS3({KEY: json.dumps({"apps": {"blog": outcome("blog")}}).encode()}, fail_get=True)
    assert drillrecord.record(config, {}, outcome(), client=broken_read) is False
    assert broken_read.puts == 0  # a read error must not overwrite the other apps' results
    assert drillrecord.record(config, {}, outcome(), client=MemoryS3(fail_put=True)) is False
    assert drillrecord.load(config, {}, client=broken_read) == {"apps": {}}
    assert drillrecord.load_for_status(config, {}, client=broken_read)["unavailable"].startswith("reading the state bucket failed")
    err = capsys.readouterr().err
    assert "reading" in err and "writing" in err and "HTTP 500" in err


def test_without_r2_credentials_recording_is_a_no_op_with_a_note(tmp_path, capsys):
    config = make_config(tmp_path)  # no cloudflare.account_id: no state bucket client
    assert drillrecord.record(config, {}, outcome()) is False
    assert "not recorded (no state bucket credentials)" in capsys.readouterr().err
    assert drillrecord.load(config, {}) == {"apps": {}}
    assert drillrecord.load_for_status(config, {}) == {"apps": {}, "unavailable": "no state bucket credentials"}


def test_an_outcome_without_an_app_is_not_recorded(tmp_path):
    store = MemoryS3()
    assert drillrecord.record(make_config(tmp_path), {}, {"outcome": "passed"}, client=store) is False
    assert store.objects == {}


def test_round_trip_through_the_strict_fake_s3(tmp_path):
    with FakeS3() as s3:
        client = S3Client(s3.endpoint, "sealedstack-state", ACCESS_KEY, SECRET_KEY)
        config = make_config(tmp_path)
        assert drillrecord.record(config, {}, outcome(), client=client)
        assert KEY in s3.objects("sealedstack-state")
        assert drillrecord.load(config, {}, client=client)["apps"]["demo-app"]["level"] == "schema"


# ---------------------------------------------------------------- rendering
def test_render_rows_one_row_per_app_with_each_outcome():
    data = {"apps": {
        "demo-app": outcome("demo-app"),
        "blog": outcome("blog", "failed", level="full", reason="pg_restore | exit 1"),
        "shop": outcome("shop", "skipped", where="runner", reason="free memory too low", backup_age_hours=None),
        "wiki": outcome("wiki", warnings=["backup is 72 h old (max_backup_age 48h)"], backup_age_hours=72.0),
    }}
    lines = drillrecord.render_rows(data, now=NOW)
    assert lines[0].startswith("| | App | Level | Where |") and lines[1].startswith("|---")
    rows = {line.split("|")[2].strip(): line for line in lines[2:]}
    assert list(rows) == ["blog", "demo-app", "shop", "wiki"]  # sorted
    assert rows["demo-app"].startswith("| ✅ ") and "| schema | server |" in rows["demo-app"] and "3 h 0 min ago" in rows["demo-app"]
    assert rows["blog"].startswith("| ❌ ") and "pg_restore \\| exit 1" in rows["blog"]  # pipes escaped
    assert rows["shop"].startswith("| ⏳ ") and "| runner |" in rows["shop"] and "| — |" in rows["shop"]
    assert "free memory too low" in rows["shop"]
    assert "⚠️ backup is 72 h old" in rows["wiki"] and "3.0 d" in rows["wiki"]


def test_render_rows_without_results_says_drills_are_off_by_default():
    lines = drillrecord.render_rows({"apps": {}})
    assert len(lines) == 1 and "no restore drill has run yet" in lines[0].lower()
    assert "drill.scheduled" in lines[0] and "backup check <app>" in lines[0]


def test_render_rows_when_the_store_is_unavailable():
    assert drillrecord.render_rows({"apps": {}, "unavailable": "no state bucket credentials"}) == [
        "drill results unavailable (no state bucket credentials)"
    ]


# ---------------------------------------------------------------- status
class _R:
    def __init__(self, id, status_, detail=""):
        self.id, self.status, self.detail, self.area, self.fix = id, status_, detail, "x", ""


def test_status_markdown_and_json_have_the_backups_section(tmp_path):
    config, data = make_config(tmp_path), {"apps": {"demo-app": outcome()}}
    md = status.render_markdown(config, "0.6.33", [_R("config.schema", "ok")], data)
    assert md.index("## Health") < md.index("## Backups") and "| ✅ | demo-app | schema | server |" in md
    payload = json.loads(status.render_json(config, "0.6.33", [], data))
    assert payload["backups"]["apps"]["demo-app"]["outcome"] == "passed"


def _run_status(tmp_path, monkeypatch, **kwargs):
    import io

    config = make_config(tmp_path)
    monkeypatch.setattr(preflight, "run_checks", lambda ctx: [_R("config.schema", "ok", "valid")])
    out = io.StringIO()
    env = {"SEALEDSTACK_CONFIG": str(config.config_dir)}
    rc = status.run(code_root=REPO, env=env, http=StubClient(), secrets=EnvSource({}), stream=out, **kwargs)
    return rc, out.getvalue()


def test_status_run_uses_the_injected_loader(tmp_path, monkeypatch):
    seen = []

    def loader(config, env):
        seen.append(config.name)
        return {"apps": {"demo-app": outcome(result="failed", reason="checksum mismatch")}}

    rc, out = _run_status(tmp_path, monkeypatch, drills_loader=loader)
    assert rc == 0 and seen == ["example-infra"]
    assert "## Backups" in out and "| ❌ | demo-app |" in out and "checksum mismatch" in out
    rc, out = _run_status(tmp_path, monkeypatch, drills_loader=loader, json_output=True)
    assert json.loads(out)["backups"]["apps"]["demo-app"]["outcome"] == "failed"


def test_status_survives_an_unavailable_store(tmp_path, monkeypatch):
    rc, out = _run_status(tmp_path, monkeypatch)  # the default loader: no R2 credentials in this config
    assert rc == 0 and "## Backups" in out and "drill results unavailable (no state bucket credentials)" in out

    def boom(config, env):
        raise RuntimeError("bucket on fire")

    rc, out = _run_status(tmp_path, monkeypatch, drills_loader=boom, json_output=True)
    assert rc == 0 and json.loads(out)["backups"] == {"apps": {}, "unavailable": "bucket on fire"}


def test_the_status_issue_shows_the_newest_drill_and_lists_a_failure_as_a_problem(tmp_path):
    config = make_config(tmp_path)
    data = {"apps": {"demo-app": outcome(result="failed", reason="pg_restore failed"), "blog": outcome("blog")}}
    body = statusissue.render_from_health(config, "0.6.33", [], drills=data)
    assert "**Backups**" in body and "| ✅ | blog |" in body and "| ❌ | demo-app |" in body
    assert body.splitlines()[1].startswith("# 🔴 ") and "`backup.drill.demo-app`" in body
    calm = statusissue.render_from_health(config, "0.6.33", [], drills_loader=lambda c, e: {"apps": {"blog": outcome("blog")}})
    assert calm.splitlines()[1].startswith("# 🟢 ") and "Problems" not in calm
    offline = statusissue.render_from_health(config, "0.6.33", [], env={})
    assert "drill results unavailable (no state bucket credentials)" in offline
