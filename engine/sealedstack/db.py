"""``db`` in Python — backup/restore/drill over the mesh (spec section 11, D-047).

Replaces ``scripts/db.sh`` and its entrypoints ``app-restore.sh`` and
``restore-drill.sh`` (and the db parts of ``move-app.sh``). Postgres runs in a
throwaway container on the app's primary server, attached to the Dokploy network
so the managed hostname resolves. The connection URL carries the password, so it
never reaches argv on either host: the libpq variables go into a 0600 file on the
server, and only non-secret names (database) are in argv (H10).
"""
from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import platformstate as platformstate_mod
from . import r2 as r2_mod
from . import secrets as secrets_mod


class DbError(RuntimeError):
    pass


def pg_env_content(url: str) -> str:
    """The libpq environment for a Postgres URL (manual parse, no URL-decode)."""
    _scheme, sep, rest = url.partition("://")
    if not sep:
        raise DbError(f"not a connection URL: {url!r}")
    userinfo, sep, hostpart = rest.rpartition("@")
    if not sep:
        raise DbError(f"connection URL has no host: {url!r}")
    user, _sep, password = userinfo.partition(":")
    hostport, _sep, database = hostpart.partition("/")
    host, _sep, port = hostport.partition(":")
    values = {
        "PGHOST": host,
        "PGPORT": port or "5432",
        "PGUSER": user,
        "PGPASSWORD": password,
        "PGDATABASE": database,
    }
    for name, value in values.items():
        if "\n" in value or "\r" in value:
            raise DbError(f"{name} contains a newline; cannot build an env file")
    return "\n".join(f"{name}={value}" for name, value in values.items())


class Transport:
    """The remote side effects (ssh, aws s3), injectable for tests."""

    def __init__(
        self, endpoint: str = "", access_key: str = "", secret_key: str = "",
        ssh_user: str = "", ssh_identity: str = "",
    ):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.ssh_user = ssh_user
        self.ssh_identity = ssh_identity

    def ssh_argv(self, server: str, remote_cmd: str) -> list:
        """``ssh`` for the server. With an identity (CI has no ssh config) it names the admin user and the CI key; without
        one it is the plain ``ssh <server>`` that relies on the operator's own ssh config."""
        if not self.ssh_identity:
            return ["ssh", server, remote_cmd]
        argv = ["ssh", "-i", self.ssh_identity, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new"]
        if self.ssh_user:
            argv += ["-l", self.ssh_user]
        return argv + [server, remote_cmd]

    def ssh(self, server: str, remote_cmd: str, stdin: Optional[bytes] = None) -> tuple[int, bytes]:
        proc = subprocess.run(self.ssh_argv(server, remote_cmd), input=stdin, capture_output=True)
        return proc.returncode, proc.stdout

    def aws(self, args: list, stdin: Optional[bytes] = None) -> tuple[int, bytes]:
        env = dict(os.environ)
        if self.access_key:
            env["AWS_ACCESS_KEY_ID"] = self.access_key
            env["AWS_SECRET_ACCESS_KEY"] = self.secret_key
        argv = ["aws"]
        if self.endpoint:
            argv += ["--endpoint-url", self.endpoint]
        argv += list(args)
        proc = subprocess.run(argv, input=stdin, capture_output=True, env=env)
        return proc.returncode, proc.stdout


class Db:
    def __init__(
        self,
        config: config_mod.Config,
        env: Optional[Mapping[str, str]] = None,
        *,
        source: Optional[secrets_mod.SecretSource] = None,
        state: Optional[platformstate_mod.PlatformState] = None,
        client: Optional[dokploy_mod.DokployClient] = None,
        transport: Optional[Transport] = None,
        out=None,
        sleep=time.sleep,
    ) -> None:
        self.config = config
        self.env = dict(env or {})
        self.out = out if out is not None else sys.stdout
        self.sleep = sleep
        self.source = source or secrets_mod.build_source(config.stack, config.config_dir, self.env)
        self.state = state or platformstate_mod.PlatformState(config, self.env, self.source)
        self.client = client or dokploy_mod.from_config(config, self.env, self.source)
        account = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
        self.bucket = self._bucket()
        if transport is None:
            try:
                creds = r2_mod.resolve(self.source, self.config, self.env)
            except r2_mod.R2Error as exc:
                raise DbError(str(exc)) from exc
            ssh_user, ssh_identity = str(config.stack.get("admin_user") or ""), self._ssh_identity()
            transport = Transport(
                endpoint=endpoint,
                access_key=creds.access_key_id,
                secret_key=creds.secret_access_key,
                ssh_user=ssh_user,
                ssh_identity=ssh_identity,
            )
        self.t = transport
        self._urls: Optional[dict] = None
        self._names: Optional[dict] = None
        self._compose_ids: Optional[dict] = None

    def _ssh_identity(self) -> str:
        """A 0600 file with the CI private key from the encrypted state, or "" when it is not there (then plain ssh)."""
        import tempfile

        from . import generated as generated_mod

        try:
            key = str(generated_mod.shared(self.config, self.env, self.source).ensure().get("ci_ssh_private_key") or "")
        except Exception:  # noqa: BLE001 - fall back to the operator's own ssh config
            return ""
        if not key:
            return ""
        fd, path = tempfile.mkstemp(prefix="sealedstack-db-key-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(key if key.endswith("\n") else key + "\n")
        os.chmod(path, 0o600)
        return path

    # -- secrets/config -----------------------------------------------------
    def _secret(self, name: str) -> str:
        result = self.source.get(self.config.secret_key(name))
        if not result.found:
            raise DbError(f"missing required secret '{name}' in the '{self.source.kind}' source")
        return result.value

    def _bucket(self) -> str:
        cfg = str((self.config.stack.get("backup") or {}).get("bucket") or "")
        if cfg:
            return cfg
        edge = Path(self.config.code_root) / ".build" / "edge-outputs.json"
        if edge.exists():
            try:
                data = json.loads(edge.read_text(encoding="utf-8") or "{}")
                value = ((data.get("backup") or {}).get("value") or {}).get("bucket")
                if value:
                    return str(value)
            except json.JSONDecodeError:
                pass
        return "sealedstack-backups"

    # -- platform outputs ---------------------------------------------------
    def _output(self, name: str) -> dict:
        return self.state.output(name) or {}

    def db_url(self, app: str) -> str:
        if self._urls is None:
            self._urls = self._output("database_urls")
        return str(self._urls.get(app) or "")

    def db_name(self, app: str) -> str:
        if self._names is None:
            self._names = self._output("database_names")
        return str(self._names.get(app) or "")

    def compose_ids(self, app: str) -> list[str]:
        if self._compose_ids is None:
            self._compose_ids = self._output("compose_ids")
        return [str(v) for v in (self._compose_ids.get(app) or {}).values()]

    # -- per-app ------------------------------------------------------------
    def primary_server(self, app: str) -> str:
        """The app's first server: from the rendered manifest when there is one, else from apps.yaml.

        A CI ``drill`` or ``restore`` job does not render first, and an empty name made ssh fail with "could not write the
        remote env file on " (live run 2026-11-01)."""
        path = Path(self.config.code_root) / ".build" / "apps" / f"{app}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            servers = data.get("servers") or []
            if servers:
                return str(servers[0])
        servers = ((self.config.apps.get("apps") or {}).get(app) or {}).get("servers") or []
        return str(servers[0]) if servers else ""

    def pg_image(self, app: str) -> str:
        path = Path(self.config.code_root) / ".build" / "apps" / f"{app}.json"
        version = "16"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            needs = ((data.get("manifest") or {}).get("needs") or {}).get("postgres") or {}
            version = str(needs.get("version") or "16")
        return f"postgres:{version}"

    def backup_prefix(self, app: str) -> str:
        return f"{app}/{self.db_name(app)}/"

    # -- object storage -----------------------------------------------------
    def latest_backup_key(self, prefix: str) -> str:
        rc, out = self.t.aws(["s3", "ls", f"s3://{self.bucket}/{prefix}"])
        if rc != 0 or not out.strip():
            return ""
        lines = [line.split() for line in out.decode("utf-8", "replace").splitlines() if line.strip()]
        lines.sort(key=lambda parts: parts[:2])
        return lines[-1][3] if lines and len(lines[-1]) >= 4 else ""

    def latest_backup(self, app: str) -> str:
        """The full key of the newest dump of the app's database, or "".

        Dokploy writes a scheduled backup to ``<its database name>/<prefix><time>.sql.gz`` (live run 2026-11-01:
        ``demo-app-db-lxunsb/demo-app/demoapp/2026-11-01T08-53-44-451Z.sql.gz``), and a rebuild gives the database a new
        name, so the dumps that matter after a rebuild sit under the *old* name. Looking only under ``<app>/<db>/`` never
        found one (the first restore drill failed with "no backups"). Dumps are matched by their ``<app>/<db>/`` directory
        under any first-level name, and the newest by the listing's date and time wins."""
        wanted = f"{app}/{self.db_name(app)}/"
        rc, out = self.t.aws(["s3", "ls", f"s3://{self.bucket}/", "--recursive"])
        if rc != 0:
            return ""
        best: tuple = ()
        for line in out.decode("utf-8", "replace").splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4:
                continue
            directory, _slash, name = parts[3].rpartition("/")
            if not name or not (f"{directory}/" == wanted or f"{directory}/".endswith("/" + wanted)):
                continue
            candidate = (parts[0], parts[1], parts[3])
            best = max(best, candidate) if best else candidate
        return best[2] if best else ""

    def s3_download(self, key: str) -> bytes:
        rc, out = self.t.aws(["s3", "cp", f"s3://{self.bucket}/{key}", "-"])
        if rc != 0:
            raise DbError(f"s3 download failed for {key}")
        return out

    def s3_upload(self, local: Path, key: str) -> bool:
        rc, _ = self.t.aws(["s3", "cp", str(local), f"s3://{self.bucket}/{key}"])
        return rc == 0

    # -- remote postgres ----------------------------------------------------
    def _remote_env(self, server: str, url: str) -> str:
        remote = 'umask 077; d="$(mktemp -d)"; printf "%s\n" "$d"; cat > "$d/env"'
        rc, out = self.t.ssh(server, remote, stdin=pg_env_content(url).encode("utf-8"))
        if rc != 0:
            raise DbError(f"could not write the remote env file on {server}")
        return out.decode("utf-8", "replace").splitlines()[0]

    def _remote_env_cleanup(self, server: str, directory: str) -> None:
        if directory:
            self.t.ssh(server, f"rm -rf -- '{directory}'")

    def psql(self, app: str, url: str, sql: str, *, value: bool = False) -> str:
        server = self.primary_server(app)
        image = self.pg_image(app)
        directory = self._remote_env(server, url)
        flags = "-w -tA -v ON_ERROR_STOP=1" if value else "-w -v ON_ERROR_STOP=1"
        rc, out = self.t.ssh(
            server,
            f"docker run --rm -i --network dokploy-network --env-file '{directory}/env' {image} psql {flags}",
            stdin=(sql + "\n").encode("utf-8"),
        )
        self._remote_env_cleanup(server, directory)
        if rc != 0:
            raise DbError(f"psql failed for {app}")
        return out.decode("utf-8", "replace").strip() if value else ""

    def wait_pg(self, app: str, url: str, *, tries: int = 60, interval: int = 2) -> bool:
        server = self.primary_server(app)
        image = self.pg_image(app)
        directory = self._remote_env(server, url)
        try:
            for _ in range(tries):
                rc, _ = self.t.ssh(
                    server,
                    f"docker run --rm --network dokploy-network --env-file '{directory}/env' {image} pg_isready -q",
                )
                if rc == 0:
                    return True
                self.sleep(interval)
            return False
        finally:
            self._remote_env_cleanup(server, directory)

    def pg_restore(self, app: str, url: str, database: str, stream: bytes) -> None:
        server = self.primary_server(app)
        image = self.pg_image(app)
        directory = self._remote_env(server, url)
        rc, _ = self.t.ssh(
            server,
            f"docker run --rm -i --network dokploy-network --env-file '{directory}/env' "
            f"{image} pg_restore --no-owner --no-acl --clean --if-exists -d '{database}'",
            stdin=stream,
        )
        self._remote_env_cleanup(server, directory)
        if rc != 0:
            raise DbError(f"pg_restore failed for {app}")

    def pg_dump(self, app: str, url: str) -> bytes:
        server = self.primary_server(app)
        image = self.pg_image(app)
        directory = self._remote_env(server, url)
        rc, out = self.t.ssh(
            server,
            f"docker run --rm -i --network dokploy-network --env-file '{directory}/env' {image} pg_dump -Fc",
        )
        self._remote_env_cleanup(server, directory)
        if rc != 0:
            raise DbError(f"pg_dump failed for {app}")
        return out

    def compose_action(self, app: str, action: str) -> None:
        for compose_id in self.compose_ids(app):
            self.client.post(f"/api/compose.{action}", {"composeId": compose_id})

    def has_deployments(self, app: str) -> bool:
        for compose_id in self.compose_ids(app):
            try:
                if self.client.deployments(compose_id):
                    return True
            except Exception:  # noqa: BLE001
                return False
        return False

    # -- entrypoints --------------------------------------------------------
    def restore(
        self,
        app: str,
        key: str = "",
        *,
        force: bool = False,
        restore_on_create: bool = False,
    ) -> int:
        url = self.db_url(app)
        if not url:
            raise DbError(f"'{app}' has no managed database")
        if not self.wait_pg(app, url):
            raise DbError(f"'{app}' Postgres did not become ready")

        tables = self.psql(app, url, "SELECT count(*) FROM pg_tables WHERE schemaname='public'", value=True)
        if (tables or "0") != "0":
            if force:
                print(f"==> {app}: database is not empty ({tables} table(s)), restoring because --force was given", file=self.out)
            elif restore_on_create:
                print(f"==> {app}: database already has {tables} table(s); skipping restore_on_create", file=self.out)
                return 0
            else:
                raise DbError(
                    f"'{app}' database is not empty ({tables} table(s) in public); refusing to overwrite it "
                    "(re-run with --force to replace it)"
                )

        if not key:
            key = self.latest_backup(app)
            if not key:
                raise DbError(f"no backups of '{app}' under s3://{self.bucket}/ (looked for */{app}/{self.db_name(app)}/)")
        elif "/" not in key:
            key = f"{self.backup_prefix(app)}{key}"

        started = {"done": False}

        def restart() -> None:
            if not started["done"]:
                print(f"==> starting {app} on {self.primary_server(app)}", file=self.out)
                self.compose_action(app, "start")
                started["done"] = True

        if self.has_deployments(app):
            print(f"==> stopping {app} on {self.primary_server(app)}", file=self.out)
            self.compose_action(app, "stop")
        else:
            print(f"==> {app}: no deployment yet; restoring without stopping", file=self.out)

        print(f"==> restoring s3://{self.bucket}/{key} into {app}", file=self.out)
        try:
            dump = gzip.decompress(self.s3_download(key))
            self.pg_restore(app, url, self.db_name(app), dump)
        finally:
            restart()
        print("==> done", file=self.out)
        return 0

    def drill(self, app: str, table: str = "") -> int:
        url = self.db_url(app)
        if not url:
            raise DbError(f"'{app}' has no managed database")
        drill_db = "sealedstack_drill"
        drill_url = f"{url.rsplit('/', 1)[0]}/{drill_db}"
        key = self.latest_backup(app)
        if not key:
            raise DbError(f"no backups of '{app}' under s3://{self.bucket}/ (looked for */{app}/{self.db_name(app)}/)")

        try:
            print(f"==> recreating {drill_db} on {self.primary_server(app)}", file=self.out)
            self.psql(app, url, f"DROP DATABASE IF EXISTS {drill_db};")
            self.psql(app, url, f"CREATE DATABASE {drill_db};")
            print(f"==> restoring {key} into {drill_db}", file=self.out)
            dump = gzip.decompress(self.s3_download(key))
            self.pg_restore(app, drill_url, drill_db, dump)
            print("==> checking", file=self.out)
            self.psql(app, drill_url, "SELECT 1;")
            if table:
                rows = self.psql(app, drill_url, f"SELECT count(*) FROM {table};", value=True)
                print(f"    {table}: {rows} rows", file=self.out)
        finally:
            try:
                self.psql(app, url, f"DROP DATABASE IF EXISTS {drill_db};")
            except DbError:
                pass
        print(f"==> drill ok for {app}", file=self.out)
        return 0

    def move_dump(self, app: str, from_server: str, to_server: str) -> bool:
        """Dump the app's DB and (best-effort) upload it for the restore path."""
        app_file = Path(self.config.code_root) / ".build" / "apps" / f"{app}.json"
        if not app_file.exists():
            raise DbError(f"no rendered manifest for '{app}' (run render first)")
        primary = str((json.loads(app_file.read_text()).get("servers") or [""])[0])
        if primary != from_server:
            raise DbError(f"'{app}' has primary '{primary}', not '{from_server}'; aborting")
        url = self.db_url(app)
        if not url:
            raise DbError(f"'{app}' has no Dokploy-managed database to move")

        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dump_dir = Path(self.config.code_root) / ".build" / "move"
        dump_dir.mkdir(parents=True, exist_ok=True)
        dump_file = dump_dir / f"{app}-{stamp}.dump.gz"
        print(f"==> dumping {app} from {from_server} over the mesh", file=self.out)
        dump_file.write_bytes(gzip.compress(self.pg_dump(app, url)))
        print(f"    wrote {dump_file}", file=self.out)

        key = f"{self.backup_prefix(app)}move-{stamp}.dump"
        uploaded = self.s3_upload(dump_file, key)
        if uploaded:
            print(f"==> uploaded s3://{self.bucket}/{key}", file=self.out)
        else:
            print(f"!! could not upload to s3://{self.bucket}; local dump at {dump_file} is still valid", file=self.out)
        print(_move_footer(app, to_server, from_server, key if uploaded else "", dump_file, self.pg_image(app)), file=self.out)
        return uploaded


def _move_footer(app: str, to_server: str, from_server: str, key: str, dump_file: Path, image: str) -> str:
    lines = [
        "sealedstack: NOTHING was changed. Finish the move by hand:",
        f"  1. Edit config/apps.yaml and reorder '{app}' so '{to_server}' is servers[0].",
        "  2. sealedstack apply  (the platform creates the database on the new primary)",
        "  3. Restore into the new database:",
    ]
    if key:
        lines.append(f"         sealedstack restore {app} {key}")
    else:
        lines.append(f"         # restore the local dump by hand: gunzip -c {dump_file} | ssh {to_server} \\")
        lines.append(f"         #   docker run --rm -i --network dokploy-network --env-file <env> {image} pg_restore -d <database>")
    lines.append(f"  4. Verify the app, then remove '{from_server}' from its servers: list and re-apply.")
    return "\n".join(lines)


def _parse_restore_args(args: list) -> tuple[str, str, bool]:
    app = key = ""
    force = False
    for arg in args:
        if arg == "--force":
            force = True
        elif not app:
            app = arg
        elif not key:
            key = arg
    return app, key, force


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    action = args[0] if args else "restore"
    rest = args[1:]
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    db = Db(config, env)
    try:
        if action == "restore":
            app, key, force = _parse_restore_args(rest)
            if not app:
                raise DbError("usage: db restore <app> [backup-key] [--force]")
            force = force or env.get("FORCE") == "1"
            restore_on_create = env.get("RESTORE_ON_CREATE") == "1"
            return db.restore(app, key, force=force, restore_on_create=restore_on_create)
        if action == "drill":
            if not rest:
                raise DbError("usage: db drill <app> [table]")
            return db.drill(rest[0], rest[1] if len(rest) > 1 else "")
        if action == "move":
            if len(rest) < 3:
                raise DbError("usage: db move <app> <from-server> <to-server>")
            db.move_dump(rest[0], rest[1], rest[2])
            return 0
        raise DbError(f"unknown db action {action!r}")
    except (DbError, config_mod.ConfigError, dokploy_mod.DokployError) as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
