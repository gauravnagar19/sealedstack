"""`check` validates the `drill:` settings beyond the JSON schema (D-085, R10)."""
from __future__ import annotations

import copy
import json

from sealedstack import preflight

from test_engine_preflight import APPS, STACK, make_config, make_ctx


def run(tmp_path, drill=None, app_drill=None, *, rendered_postgres=None, apps=None):
    stack = copy.deepcopy(STACK)
    if drill is not None:
        stack["drill"] = drill
    apps = copy.deepcopy(apps if apps is not None else APPS)
    if app_drill is not None:
        apps["apps"]["hello"]["drill"] = app_drill
    config = make_config(tmp_path, stack=stack, apps=apps)
    if rendered_postgres is not None:  # what check_config's render leaves in the build dir
        out = tmp_path / "build" / "apps"
        out.mkdir(parents=True, exist_ok=True)
        needs = {"postgres": {"version": "16"}} if rendered_postgres else {}
        (out / "hello.json").write_text(json.dumps({"name": "hello", "manifest": {"needs": needs}}))
    return {r.id: r for r in preflight.check_drill_settings(make_ctx(tmp_path, config))}


def test_registered_and_silent_without_drill_settings(tmp_path):
    assert preflight.check_drill_settings in preflight.AREA_CHECKS
    assert run(tmp_path) == {}  # drills are off by default (R1): nothing to validate


def test_valid_settings_pass(tmp_path):
    results = run(tmp_path, {"level": "schema", "where": "runner", "max_backup_age": "48h",
                             "limits": {"timeout": "30m", "memory": "1g"}},
                  {"level": "verify", "tables": ["notes", "public.users"]})
    assert results["backup.drill"].status == "ok" and results["backup.drill"].area == "backup"


def test_runner_needs_a_light_stack_level(tmp_path):
    result = run(tmp_path, {"where": "runner", "level": "tables"})["backup.drill"]
    assert result.status == "fail" and "stack.yaml drill: where: runner" in result.detail and "tables" in result.detail


def test_runner_needs_a_light_level_per_app_override(tmp_path):
    result = run(tmp_path, {"where": "runner"}, {"level": "full"})["backup.drill"]
    assert result.status == "fail" and "hello.drill: level full cannot run with where: runner" in result.detail
    result = run(tmp_path, {"level": "sample"}, {"where": "runner"})["backup.drill"]
    assert result.status == "fail" and "hello.drill: level sample" in result.detail
    # an app that moves itself back to the server is fine
    assert run(tmp_path, {"where": "runner"}, {"level": "full", "where": "server"})["backup.drill"].status == "ok"


def test_table_names_must_look_like_table_names(tmp_path):
    result = run(tmp_path, None, {"tables": ["notes", "public.users", "drop table x;", "a.b.c", "9lives"]})["backup.drill"]
    assert result.status == "fail"
    for bad in ("'drop table x;'", "'a.b.c'", "'9lives'"):
        assert bad in result.detail
    assert "'notes'" not in result.detail and "'public.users'" not in result.detail
    assert "must be a list" in run(tmp_path, None, {"tables": "notes"})["backup.drill"].detail


def test_durations_must_parse(tmp_path):
    result = run(tmp_path, {"max_backup_age": "two days", "limits": {"timeout": 30}})["backup.drill"]
    assert result.status == "fail"
    assert "max_backup_age 'two days'" in result.detail and "limits.timeout '30'" in result.detail
    assert "0m" in run(tmp_path, {"limits": {"timeout": "0m"}})["backup.drill"].detail  # zero is no timeout
    assert run(tmp_path, {"limits": {"timeout": "1h30m"}, "max_backup_age": "2d"})["backup.drill"].status == "ok"
    assert "hello.drill: max_backup_age" in run(tmp_path, None, {"max_backup_age": "soon"})["backup.drill"].detail


def test_scheduled_without_a_managed_database_warns(tmp_path):
    result = run(tmp_path, {"scheduled": True}, rendered_postgres=False)["backup.drill_scheduled"]
    assert result.status == "warn" and "no app has a managed database" in result.detail
    assert "backup.drill_scheduled" in run(tmp_path, {"scheduled": True}, apps={"apps": {}})


def test_scheduled_with_a_managed_database_is_quiet(tmp_path):
    assert "backup.drill_scheduled" not in run(tmp_path, {"scheduled": True}, rendered_postgres=True)
    assert "backup.drill_scheduled" not in run(tmp_path, {"scheduled": False}, rendered_postgres=False)
    # nothing rendered (offline): an app may still have a database, so no guess
    assert "backup.drill_scheduled" not in run(tmp_path / "offline", {"scheduled": True})


def test_scheduled_with_only_drill_disabled_databases_warns(tmp_path):
    results = run(tmp_path, {"scheduled": True}, {"enabled": False}, rendered_postgres=True)
    assert results["backup.drill_scheduled"].status == "warn"
