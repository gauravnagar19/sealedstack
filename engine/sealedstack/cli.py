"""The ``sealedstack <noun> <verb>`` CLI (spec 6.5, D-042, D-047).

This first increment makes ``check`` fully native and delegates every not-yet-
ported command to the existing ``scripts/`` so the surface stays usable while the
rest of the orchestration moves from bash to Python.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import __version__
from . import config as cfg
from . import orchestrate
from . import preflight
from . import status as status_mod

PROG = "sealedstack"

# Flat CI-vocabulary aliases (user-experience.md 12.1) -> canonical noun/verb.
ALIASES = {
    "add-server": "server add",
    "add-app": "app add",
    "restore": "db restore",
    "drill": "db drill",
}


@dataclass
class Action:
    """What a parsed command should do."""

    command: str
    native: bool = False
    argv: list[list[str]] = field(default_factory=list)
    handler: Optional[Callable] = None


def _add_globals(parser: argparse.ArgumentParser) -> None:
    # SUPPRESS defaults let the flags be given before *or* after the noun without
    # one parser clobbering the other's value.
    parser.add_argument("--config", metavar="DIR", default=argparse.SUPPRESS,
                        help="config directory (default: $SEALEDSTACK_CONFIG or <repo>/config)")
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable JSON output (check/plan/status)")
    parser.add_argument("--break-glass", action="store_true", default=argparse.SUPPRESS,
                        help="allow a local write command, guarded (spec 6.6)")
    parser.add_argument("--force-unlock", metavar="ID", default=argparse.SUPPRESS,
                        help="clear a stale remote state lock by id (D-045 / C1)")


def _leaf(parent: argparse._SubParsersAction, name: str, help: str = "") -> argparse.ArgumentParser:
    parser = parent.add_parser(name, help=help)
    _add_globals(parser)
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="sealedstack — build and operate a sealed origin",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "commands:\n"
            "  setup --domain D --email E --server S   write config/ and open the Setup PR (no prompts)\n"
            "  check                 preflight: verify everything before changing anything\n"
            "  up                    converge everything (state -> edge -> servers -> platform -> status)\n"
            "  plan                  show the plan (creates, changes, destroys)\n"
            "  apply                 apply the merged config (CI/approved; local needs --break-glass)\n"
            "  status                show health, versions, apps and drift\n"
            "  drift                 read-only plan: report changes made outside git\n"
            "  ci resolve|exec       what the infra repo's CI does for an event (infra.yml)\n"
            "  server add <name>     bootstrap/register a server\n"
            "  server sync [name]    re-assert the host baseline\n"
            "  app add               onboard an app\n"
            "  db restore <app>      restore a database backup\n"
            "  db drill <app>        restore into a throwaway database and check it\n"
            "  debug render|publish|providers|onboard|ci-export|init   internals (init = the interactive wizard)\n"
            "\n"
            "run `sealedstack <command> --help` for a command's options.\n"
        ),
    )
    _add_globals(parser)
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    sub = parser.add_subparsers(dest="noun", title="commands")

    for name, help in (
        ("setup", "write config/ and open the Setup PR/MR"),
        ("check", "preflight: verify everything before changing anything"),
        ("up", "converge everything"),
        ("plan", "show the plan"),
        ("apply", "apply the merged config"),
        ("status", "show health, versions, apps and drift"),
        ("drift", "read-only plan: report changes made outside git"),
    ):
        _leaf(sub, name, help).add_argument("extras", nargs="*")

    status_leaf = sub.choices["status"]
    status_leaf.add_argument("--show-dokploy-admin", action="store_true",
                             help="print the generated Dokploy admin login once (local --break-glass session only)")

    server = _leaf(sub, "server", "servers")
    server_sub = server.add_subparsers(dest="verb", title="server commands")
    server_add = _leaf(server_sub, "add", "bootstrap/register a server")
    server_add.add_argument("name")
    server_sync = _leaf(server_sub, "sync", "re-assert the host baseline")
    server_sync.add_argument("name", nargs="?")

    app = _leaf(sub, "app", "apps")
    app_sub = app.add_subparsers(dest="verb", title="app commands")
    _leaf(app_sub, "add", "onboard an app").add_argument("extras", nargs="*")

    ci = _leaf(sub, "ci", "what the infra repo's CI does for an event (used by infra.yml)")
    ci_sub = ci.add_subparsers(dest="verb", title="ci commands")
    for verb, help in (("resolve", "turn the event into one command"), ("exec", "run it and report")):
        _leaf(ci_sub, verb, help).add_argument("extras", nargs="*")

    debug = _leaf(sub, "debug", "internals for troubleshooting")
    debug_sub = debug.add_subparsers(dest="verb", title="debug commands")
    for verb in ("render", "publish", "providers", "onboard", "ci-export", "init"):
        _leaf(debug_sub, verb, verb).add_argument("extras", nargs="*")

    db = _leaf(sub, "db", "databases")
    db_sub = db.add_subparsers(dest="verb", title="db commands")
    for verb, help in (("restore", "restore a database backup"), ("drill", "restore into a throwaway database")):
        leaf = _leaf(db_sub, verb, help)
        leaf.add_argument("app")
        leaf.add_argument("extras", nargs="*")

    # Flat aliases (same grammar as their noun/verb form).
    add_server = _leaf(sub, "add-server", "alias for `server add`")
    add_server.add_argument("name")
    _leaf(sub, "add-app", "alias for `app add`").add_argument("extras", nargs="*")
    for verb, help in (("restore", "alias for `db restore`"), ("drill", "alias for `db drill`")):
        leaf = _leaf(sub, verb, help)
        leaf.add_argument("app")
        leaf.add_argument("extras", nargs="*")
    _leaf(sub, "approve-check", "enforce the D-052 approval gate for a write command").add_argument("command")
    return parser


# Commands whose own flags (``--name``, ``--repo``, ...) belong to the command, not
# to this parser. Value: how many positionals the parser itself consumes first.
PASSTHROUGH = {
    "setup": 0,
    "add-app": 0,
    "app add": 0,
    "debug render": 0,
    "debug publish": 0,
    "debug providers": 0,
    "debug onboard": 0,
    "debug ci-export": 0,
    "debug init": 0,
    "ci resolve": 0,
    "ci exec": 0,
    "restore": 1,
    "drill": 1,
    "db restore": 1,
    "db drill": 1,
}
_GLOBAL_WITH_VALUE = {"--config", "--force-unlock"}
_GLOBAL_FLAGS = {"--json", "--break-glass"}


def split_passthrough(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split ``argv`` into what this parser reads and the command's own flags.

    ``sealedstack app add --name x --repo y`` must reach ``appadd`` untouched, in
    order. Global flags given before the command are still honoured.
    """
    tokens = list(argv)
    i, head = 0, []
    while i < len(tokens):
        token = tokens[i]
        if token in _GLOBAL_WITH_VALUE and i + 1 < len(tokens):
            head += tokens[i : i + 2]
            i += 2
        elif token in _GLOBAL_FLAGS or token.startswith("--config=") or token.startswith("--force-unlock="):
            head.append(token)
            i += 1
        else:
            break
    if i >= len(tokens):
        return tokens, []
    noun = tokens[i]
    verb = tokens[i + 1] if i + 1 < len(tokens) and noun in ("app", "db", "debug", "server", "ci") else ""
    command = f"{noun} {verb}".strip() if verb else noun
    if command not in PASSTHROUGH:
        return tokens, []
    consumed = i + (2 if verb else 1)
    positionals = PASSTHROUGH[command]
    taken = 0
    while taken < positionals and consumed < len(tokens) and not tokens[consumed].startswith("-"):
        consumed += 1
        taken += 1
    return tokens[:consumed], tokens[consumed:]


def canonical(args: argparse.Namespace) -> str:
    noun = getattr(args, "noun", None)
    if not noun:
        return ""
    if noun in ALIASES:
        return ALIASES[noun]
    verb = getattr(args, "verb", None)
    if noun in ("server", "app", "db", "debug", "ci") and verb:
        return f"{noun} {verb}"
    return noun


def _script(code_root: Path, name: str) -> str:
    return str(code_root / "scripts" / name)


def r2_endpoint(account: str) -> str:
    """The R2 S3 endpoint (a function so tests can point it at a local fake)."""
    return f"https://{account}.r2.cloudflarestorage.com"


def state_bucket(config) -> str:
    """The R2 state bucket (same default as ``tofu.backend_config``)."""
    return str((config.stack.get("state") or {}).get("bucket") or "sealedstack-state")


def _r2_client(config, env, http=None, *, purpose: str = "remote state lock"):
    """An S3 client for the R2 state bucket, or None when R2 credentials are absent."""
    if config is None:
        return None
    try:
        from . import r2 as r2_mod
        from . import s3 as s3_mod
        from . import secrets as secrets_mod

        account = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
        if not account:
            return None
        source = secrets_mod.build_source(config.stack, config.config_dir, env)
        try:
            creds = r2_mod.resolve(source, config, env, http=http, required=False)
        except r2_mod.R2Error as exc:
            # Say why (never any key material).
            print(f"{PROG}: {purpose} unavailable: {str(exc).splitlines()[0]}", file=sys.stderr)
            return None
        if creds is None:
            return None
        bucket = state_bucket(config)
        return s3_mod.S3Client(
            r2_endpoint(account), bucket, creds.access_key_id, creds.secret_access_key
        )
    except Exception:  # noqa: BLE001 - fall back to the local lock / local progress
        return None


def _state_lock(config, env, http=None, *, kind: str = "break-glass"):
    """Build the remote R2 state lock, or None when R2 credentials are absent.

    ``kind`` names the holder in the lock body (``ci`` or ``break-glass``)."""
    client = _r2_client(config, env, http)
    if client is None:
        return None
    from . import lock as lock_mod

    key = lock_mod.lock_key(config.name)
    return lock_mod.StateLock(
        lock_mod.S3LockBackend(client, key),
        owner=lock_mod.lock_owner(env, kind),
        key=key,
        run_url=lock_mod.run_url(env),
    )


def _progress(config, env, command: str, workdir: str, *, client=None):
    """The record of finished `up` phases: R2 (shared across CI runs) plus a local mirror."""
    from . import progress as progress_mod

    client = client if client is not None else _r2_client(config, env, purpose="remote progress record")
    key = f"sealedstack/{(config.name if config else '') or 'sealedstack'}/progress-{command}.json"
    stores = [progress_mod.S3Store(client, key) if client is not None else None,
              progress_mod.FileStore(Path(workdir) / f"phases-{command}.json")]
    commit = env.get("GITHUB_SHA") or env.get("SEALEDSTACK_COMMIT", "")
    return progress_mod.Progress(stores, commit=commit)


def _orchestrate_handler(command: str) -> Callable:
    """A native handler that runs the Python phase list for plan/apply/up."""

    def handler(config, env, code_root, json_output, runner):
        servers = list((config.servers.get("servers") or {}).keys()) if config else []
        # `up` bootstraps the first configured server; additional servers are
        # added explicitly with `server add`.
        up_servers = servers[:1] if command == "up" else []
        workdir = env.get("SEALEDSTACK_WORKDIR") or str(code_root / ".sealedstack")
        progress = _progress(config, env, command, workdir) if command == "up" else None
        state_path = Path(workdir) / f"phases-{command}.json"
        return orchestrate.run_phases(
            command,
            code_root=code_root,
            runner=runner,
            stack_name=(config.name if config else ""),
            servers=up_servers,
            json_output=json_output,
            state_path=None if progress is not None else state_path,
            progress=progress,
        )

    return handler


def _drift_handler(config, env, code_root, json_output, runner):
    from . import drift as drift_mod
    from . import forge as forge_mod

    forge_obj, repo = None, env.get("GITHUB_REPOSITORY", "")
    if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
        try:
            forge_obj = forge_mod.GitHubForge.from_env(env)
            repo = repo or forge_mod.repo_from_git(code_root)
        except Exception:  # noqa: BLE001 - report to stderr only
            forge_obj = None
    return drift_mod.run(code_root=code_root, env=env, forge=forge_obj, repo=repo)


def _status_handler(config, env, code_root, json_output, runner):
    return status_mod.run(code_root=code_root, env=env, json_output=json_output)


def _show_admin_handler(break_glass: bool) -> Callable:
    def handler(config, env, code_root, json_output, runner):
        from . import generated as generated_mod
        from . import secrets as secrets_mod

        if config is None:
            print(f"{PROG}: no config found", file=sys.stderr)
            return 2
        source = secrets_mod.build_source(config.stack, config.config_dir, env)
        is_ci = cfg.Runtime.detect(code_root, env).is_ci
        try:
            return generated_mod.show_admin(config, env, source, is_ci=is_ci, break_glass=break_glass)
        except cfg.ConfigError as exc:
            print(f"{PROG}: {exc}", file=sys.stderr)
            return 1

    return handler


def _approve_handler(write_command: str) -> Callable:
    """Enforce the D-052 approval gate for a write command (used by CI)."""

    def handler(config, env, code_root, json_output, runner):
        from . import approval as approval_mod
        from . import forge as forge_mod

        approvers = config.approvers() if config else []
        request = approval_mod.env_request(env)
        try:
            f = forge_mod.GitHubForge.from_env(env)
        except forge_mod.ForgeError as exc:
            print(f"approve-check: {exc}", file=sys.stderr)
            return 1
        decision = approval_mod.check(
            f,
            request["repo"],
            write_command,
            commit_sha=request["commit_sha"],
            actor=request["actor"],
            approvers=approvers,
            pr_number=request["pr_number"],
            ref=request["ref"],
            branch=request["branch"],
        )
        if decision.approved:
            print(f"approve-check: approved — {decision.reason}", file=sys.stderr)
            return 0
        print(f"approve-check: refused — {decision.reason}", file=sys.stderr)
        return 1

    return handler


def plan_action(args: argparse.Namespace, *, code_root: Path) -> Action:
    """Turn parsed args into an Action (native, or a list of legacy argv)."""
    command = canonical(args)
    if command == "check":
        return Action(command, native=True)

    python = sys.executable or "python3"
    extras = list(getattr(args, "extras", []) or [])

    if command == "setup":
        # Non-interactive Setup job (UX §4); the wizard is `debug init`.
        return Action(command, argv=[[python, "-m", "sealedstack.setupjob", *extras]])
    if command == "debug init":
        return Action(command, argv=[[python, _script(code_root, "init.py"), *extras]])
    if command == "plan":
        return Action(command, handler=_orchestrate_handler("plan"))
    if command == "apply":
        return Action(command, handler=_orchestrate_handler("apply"))
    if command == "up":
        return Action(command, handler=_orchestrate_handler("up"))
    if command == "status":
        if getattr(args, "show_dokploy_admin", False):
            return Action(command, handler=_show_admin_handler(bool(getattr(args, "break_glass", False))))
        return Action(command, handler=_status_handler)
    if command == "drift":
        return Action(command, handler=_drift_handler)
    if command == "approve-check":
        return Action(command, handler=_approve_handler(getattr(args, "command", "")))
    if command == "server add":
        # The public IP is a bootstrap secret, never a job input (D-049).
        return Action(command, argv=[[python, "-m", "sealedstack.server", "up", args.name]])
    if command == "server sync":
        argv = [python, "-m", "sealedstack.server", "sync"]
        if getattr(args, "name", None):
            argv.append(args.name)
        return Action(command, argv=[argv])
    if command == "app add":
        return Action(command, argv=[[python, "-m", "sealedstack.appadd", *extras]])
    if command == "db restore":
        return Action(command, argv=[[python, "-m", "sealedstack.db", "restore", args.app, *extras]])
    if command == "db drill":
        return Action(command, argv=[[python, "-m", "sealedstack.db", "drill", args.app, *extras]])
    if command == "debug render":
        return Action(command, argv=[[python, _script(code_root, "render.py"), *extras]])
    if command == "debug publish":
        return Action(command, argv=[[python, "-m", "sealedstack.publishsettings", *extras]])
    if command == "debug providers":
        return Action(command, argv=[[python, _script(code_root, "registry.py"), "list", *extras]])
    if command == "debug onboard":
        return Action(command, argv=[[python, "-m", "sealedstack.onboard", *extras]])
    if command in ("ci resolve", "ci exec"):
        return Action(command, argv=[[python, "-m", "sealedstack.infraci", command.split()[1], *extras]])
    if command == "debug ci-export":
        return Action(command, argv=[[python, "-m", "sealedstack.ciexport", *extras]])
    return Action(command)


def default_runner(argv: Sequence[str], code_root: Path) -> int:
    """Run a command in the CALLER's working directory.

    In the engine image that is the mounted infra repo (``WORKDIR /infra``). Commands that
    work on the repo they were started in (``setup`` reads ``sealedstack.version`` there,
    ``debug onboard`` defaults to "the current directory") would otherwise look inside the
    engine's own tree. Nothing needs ``code_root`` as its working directory: the engine is
    importable through ``PYTHONPATH`` and the scripts find their siblings by their own path.
    ``code_root`` stays in the signature because every runner shares it.
    """
    proc = subprocess.run(list(argv))
    return proc.returncode


def execute(
    action: Action,
    *,
    code_root: Path,
    config=None,
    env=None,
    ctx: Optional[preflight.CheckContext] = None,
    json_output: bool = False,
    version: str = "",
    runner: Optional[Callable[[Sequence[str], Path], int]] = None,
) -> int:
    run = runner or default_runner

    if action.handler is not None:
        return action.handler(config, env or {}, code_root, json_output, run)

    if action.native:
        assert ctx is not None, "native command needs a check context"
        results = preflight.run_checks(ctx)
        if json_output:
            print(preflight.render_json(results, version=version))
        else:
            print(preflight.render_markdown(results))
        return 0 if all(result.status != "fail" for result in results) else 1

    for argv in action.argv:
        rc = run(argv, code_root)
        if rc != 0:
            return rc
    return 0


def run(
    argv: Sequence[str],
    *,
    code_root: Optional[Path] = None,
    env: Optional[dict] = None,
    git: Optional[cfg.GitRunner] = None,
    http=None,
    secrets=None,
    runner: Optional[Callable[[Sequence[str], Path], int]] = None,
    confirm_fn: Optional[Callable[[str], str]] = None,
    audit_fn: Optional[Callable[[str], None]] = None,
    tcp_probe: Optional[Callable[[str, int, float], bool]] = None,
) -> int:
    parser = build_parser()
    head, tail = split_passthrough(argv)
    args = parser.parse_args(head)
    if tail:
        args.extras = list(getattr(args, "extras", []) or []) + tail
    env = dict(os.environ) if env is None else dict(env)
    code_root = Path(code_root) if code_root else cfg.find_code_root(env)

    command = canonical(args)
    if not command:
        parser.print_help()
        return 0

    json_output = bool(getattr(args, "json", False))
    break_glass = bool(getattr(args, "break_glass", False))

    runtime = cfg.Runtime.detect(code_root, env, git)

    try:
        config_dir = cfg.resolve_config_dir(getattr(args, "config", None), env, code_root)
        config = cfg.load_config(config_dir, code_root, env)
    except cfg.ConfigError:
        config = None
    stack_name = config.name if config else ""

    force_unlock = getattr(args, "force_unlock", None)
    if force_unlock:
        lock_obj = _state_lock(config, env, http)
        if lock_obj is None:
            print(f"{PROG}: no R2 state lock configured (need R2 credentials)", file=sys.stderr)
            return 2
        ok = lock_obj.force_unlock(force_unlock)
        print(f"{PROG}: {'cleared stale state lock' if ok else 'no lock with that id'}", file=sys.stderr)
        return 0 if ok else 1

    if command in cfg.WRITE_COMMANDS:
        forge_obj = None
        repo = env.get("GITHUB_REPOSITORY", "")
        if env.get("GH_TOKEN") or env.get("GITHUB_TOKEN"):
            try:
                from . import forge as forge_mod

                forge_obj = forge_mod.GitHubForge.from_env(env)
                repo = repo or forge_mod.repo_from_git(code_root)
            except Exception:  # noqa: BLE001 - audit falls back to stderr
                forge_obj = None
        try:
            runtime.guard_write(
                command,
                stack_name,
                break_glass=break_glass,
                confirm_fn=confirm_fn,
                audit_fn=audit_fn,
                forge=forge_obj,
                repo=repo,
                lock=_state_lock(config, env, http),
            )
        except cfg.BreakGlassError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    action = plan_action(args, code_root=code_root)
    if not action.argv and not action.native and action.handler is None:
        print(f"{PROG}: '{command}' is not implemented yet", file=sys.stderr)
        return 2

    ctx = None
    if action.native:
        if config is None:
            print(f"{PROG}: no config found; run `{PROG} setup` first", file=sys.stderr)
            return 2
        ctx = preflight.build_context(
            config,
            env=env,
            http=http,
            secrets=secrets,
            version=cfg.read_version(code_root),
            tcp_probe=tcp_probe,
        )
    return execute(
        action,
        code_root=code_root,
        config=config,
        env=env,
        ctx=ctx,
        json_output=json_output,
        version=cfg.read_version(code_root),
        runner=runner,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
