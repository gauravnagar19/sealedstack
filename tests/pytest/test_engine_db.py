"""db port (Task 4.9 / D-047)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, db as dbmod, secrets as secrets_mod
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


class FakeState:
    def __init__(self, urls, names, compose_ids):
        self._data = {"database_urls": urls, "database_names": names, "compose_ids": compose_ids}

    def output(self, name):
        return self._data.get(name, {})


class FakeClient:
    def __init__(self, deployments=None):
        self._deployments = deployments or {}
        self.posts: list[tuple] = []

    def deployments(self, compose_id):
        return self._deployments.get(compose_id, [])

    def post(self, path, body=None):
        self.posts.append((path, body))
        return {}


class FakeTransport:
    def __init__(self, psql_value="3"):
        self.psql_value = psql_value
        self.calls: list[tuple] = []

    def ssh(self, server, remote_cmd, stdin=None):
        self.calls.append((server, remote_cmd, stdin))
        if "cat > " in remote_cmd:  # write the remote env file
            return 0, b"/tmp/sst\n"
        if "pg_isready" in remote_cmd:
            return 0, b""
        if "psql" in remote_cmd and "-tA" in remote_cmd:
            return 0, (self.psql_value + "\n").encode()
        if "psql" in remote_cmd:
            return 0, b""
        if "pg_restore" in remote_cmd:
            return 0, b""
        if "rm -rf" in remote_cmd:
            return 0, b""
        return 0, b""

    def aws(self, args, stdin=None):
        self.calls.append(("aws", args, stdin))
        return 0, b""


def make_config(tmp_path: Path) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump({"name": "example-infra", "cloudflare": {"account_id": "acct"}, "slots": {"secrets": "env"}})
    )
    (cfg_dir / "apps.yaml").write_text("{}\n")
    (cfg_dir / "servers.yaml").write_text("servers: {}\n")
    apps = tmp_path / ".build" / "apps"
    apps.mkdir(parents=True)
    (apps / "shop.json").write_text(json.dumps({"name": "shop", "servers": ["web-1"], "manifest": {"needs": {"postgres": {"version": 16}}}}))
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


def make_db(tmp_path, *, psql_value="3", source=None, deployments=None):
    config = make_config(tmp_path)
    state = FakeState(
        {"shop": "postgres://u:p@db:5432/shop"},
        {"shop": "shop"},
        {"shop": {"web-1": "compose-1"}},
    )
    return dbmod.Db(
        config,
        {},
        source=source or FakeSource({}),
        state=state,
        client=FakeClient(deployments),
        transport=FakeTransport(psql_value),
        out=io.StringIO(),
    )


def test_pg_env_content():
    assert dbmod.pg_env_content("postgres://u:p@db:5432/shop") == (
        "PGHOST=db\nPGPORT=5432\nPGUSER=u\nPGPASSWORD=p\nPGDATABASE=shop"
    )
    with pytest.raises(dbmod.DbError):
        dbmod.pg_env_content("not-a-url")


def test_latest_backup_key(tmp_path):
    db = make_db(tmp_path)
    db.t.aws = lambda args, stdin=None: (0, b"2026-10-11 03:00:00  10  a.dump\n2026-10-12 03:00:00  10  b.dump\n")
    assert db.latest_backup_key("shop/shop/") == "b.dump"


def test_restore_refuses_nonempty_without_force(tmp_path):
    db = make_db(tmp_path, psql_value="3")
    with pytest.raises(dbmod.DbError) as exc:
        db.restore("shop")
    assert "not empty" in str(exc.value)


def test_restore_on_create_skips_nonempty(tmp_path):
    db = make_db(tmp_path, psql_value="3")
    assert db.restore("shop", restore_on_create=True) == 0


LISTING = (
    b"2026-11-01 06:00:01        397 demo-app-db-ztqktn/demo-app/demoapp/2026-11-01T06-00-00-059Z.sql.gz\n"
    b"2026-11-01 08:53:45       1058 demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz\n"
    b"2026-11-01 07:00:00         90 other-db-abc/other/otherdb/2026-11-01T07-00-00-000Z.sql.gz\n"
    b"2026-10-31 01:00:00         10 demo-app/demoapp/move-20261031T010000Z.dump\n"
)


def test_latest_backup_finds_dokploys_layout_under_any_database_name(tmp_path):
    """Live run 2026-11-01: Dokploy stores <database name>/<prefix><time>.sql.gz; the drill looked under <prefix> only."""
    db = make_db(tmp_path)
    db._names = {"demo-app": "demoapp"}
    db.t.aws = lambda args, stdin=None: (0, LISTING)
    assert db.latest_backup("demo-app") == "demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz"


def test_the_dump_of_a_replaced_database_is_still_found_after_a_rebuild(tmp_path):
    db = make_db(tmp_path)
    db._names = {"demo-app": "demoapp"}
    old_only = LISTING.split(b"\n")[0] + b"\n" + LISTING.split(b"\n")[3] + b"\n"
    db.t.aws = lambda args, stdin=None: (0, old_only)
    assert db.latest_backup("demo-app").startswith("demo-app-db-ztqktn/")


def test_no_matching_dump_or_a_failed_listing_gives_nothing(tmp_path):
    db = make_db(tmp_path)
    db._names = {"demo-app": "demoapp"}
    db.t.aws = lambda args, stdin=None: (0, b"2026-11-01 07:00:00  90 other-db-abc/other/otherdb/x.sql.gz\n")
    assert db.latest_backup("demo-app") == ""
    db.t.aws = lambda args, stdin=None: (1, b"")
    assert db.latest_backup("demo-app") == ""


def test_the_primary_server_comes_from_apps_yaml_when_nothing_was_rendered(tmp_path):
    """Live run 2026-11-01: a CI `drill` job does not render; the empty server name made ssh fail."""
    db = make_db(tmp_path)
    db.config.apps["apps"] = {"shop": {"servers": ["web-1", "web-2"]}}
    assert db.primary_server("shop") == "web-1"
    assert db.primary_server("unknown") == ""


def test_ssh_names_the_admin_user_and_the_ci_key_when_it_has_an_identity():
    """Live run 2026-11-01: `ssh server-1` has no ssh config in CI; the drill could not write its env file."""
    ci = dbmod.Transport(ssh_user="admin", ssh_identity="/tmp/key").ssh_argv("server-1", "true")
    assert ci[:3] == ["ssh", "-i", "/tmp/key"] and ["-l", "admin"] == ci[ci.index("-l"):ci.index("-l") + 2]
    assert ci[-2:] == ["server-1", "true"] and "BatchMode=yes" in ci and "IdentitiesOnly=yes" in ci
    assert dbmod.Transport().ssh_argv("server-1", "true") == ["ssh", "server-1", "true"]


def _drill_all_db(tmp_path, urls, failing=()):
    db = make_db(tmp_path)
    db._urls = urls
    ran = []

    def fake_drill(app, table="", level=""):
        ran.append((app, table))
        if app in failing:
            raise dbmod.DbError("no backups")
        return 0

    db.drill = fake_drill
    return db, ran


def test_drill_all_drills_every_app_with_a_managed_database_and_fails_if_one_does(tmp_path):
    db, ran = _drill_all_db(tmp_path, {"b": "postgres://x", "a": "postgres://y"})
    db._output = lambda name: db._urls
    assert db.drill_all("notes") == 0 and ran == [("a", "notes"), ("b", "notes")]
    (tmp_path / "f").mkdir()
    db, ran = _drill_all_db(tmp_path / "f", {"a": "u", "b": "u"}, failing=("a",))
    db._output = lambda name: db._urls
    assert db.drill_all() == 1 and [app for app, _ in ran] == ["a", "b"]  # one failure does not stop the others


def test_drill_all_with_no_database_is_not_an_error(tmp_path):
    db, ran = _drill_all_db(tmp_path, {})
    db._output = lambda name: {}
    assert db.drill_all() == 0 and ran == []
