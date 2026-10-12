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
            "  setup                 write config/ and open the Setup PR/MR\n"
            "  check                 preflight: verify everything before changing anything\n"
            "  up                    converge everything (state -> edge -> servers -> platform -> status)\n"
            "  plan                  show the plan (creates, changes, destroys)\n"
            "  apply                 apply the merged config (CI/approved; local needs --break-glass)\n"
            "  status                show health, versions, apps and drift\n"
            "  server add <name>     bootstrap/register a server\n"
            "  server sync [name]    re-assert the host baseline\n"
            "  app add               onboard an app\n"
            "  db restore <app>      restore a database backup\n"
            "  db drill <app>        restore into a throwaway database and check it\n"
            "  debug render|publish|providers   internals for troubleshooting\n"
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
    ):
        _leaf(sub, name, help).add_argument("extras", nargs="*")

    server = _leaf(sub, "server", "servers")
    server_sub = server.add_subparsers(dest="verb", title="server commands")
    server_add = _leaf(server_sub, "add", "bootstrap/register a server")
    server_add.add_argument("name")
    server_sync = _leaf(server_sub, "sync", "re-assert the host baseline")
    server_sync.add_argument("name", nargs="?")

    app = _leaf(sub, "app", "apps")
    app_sub = app.add_subparsers(dest="verb", title="app commands")
    _leaf(app_sub, "add", "onboard an app").add_argument("extras", nargs="*")

    debug = _leaf(sub, "debug", "internals for troubleshooting")
    debug_sub = debug.add_subparsers(dest="verb", title="debug commands")
    for verb in ("render", "publish", "providers"):
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
    return parser


def canonical(args: argparse.Namespace) -> str:
    noun = getattr(args, "noun", None)
    if not noun:
        return ""
    if noun in ALIASES:
        return ALIASES[noun]
    verb = getattr(args, "verb", None)
    if noun in ("server", "app", "db", "debug") and verb:
        return f"{noun} {verb}"
    return noun


def _script(code_root: Path, name: str) -> str:
    return str(code_root / "scripts" / name)


def _orchestrate_handler(command: str) -> Callable:
    """A native handler that runs the Python phase list for plan/apply/up."""

    def handler(config, env, code_root, json_output, runner):
        servers = list((config.servers.get("servers") or {}).keys()) if config else []
        # `up` bootstraps the first configured server; additional servers are
        # added explicitly with `server add`.
        up_servers = servers[:1] if command == "up" else []
        return orchestrate.run_phases(
            command,
            code_root=code_root,
            runner=runner,
            stack_name=(config.name if config else ""),
            servers=up_servers,
            json_output=json_output,
        )

    return handler


def _status_handler(config, env, code_root, json_output, runner):
    return status_mod.run(code_root=code_root, env=env, json_output=json_output)


def plan_action(args: argparse.Namespace, *, code_root: Path) -> Action:
    """Turn parsed args into an Action (native, or a list of legacy argv)."""
    command = canonical(args)
    if command == "check":
        return Action(command, native=True)

    python = sys.executable or "python3"
    extras = list(getattr(args, "extras", []) or [])

    if command == "setup":
        return Action(command, argv=[[python, _script(code_root, "init.py"), *extras]])
    if command == "plan":
        return Action(command, handler=_orchestrate_handler("plan"))
    if command == "apply":
        return Action(command, handler=_orchestrate_handler("apply"))
    if command == "up":
        return Action(command, handler=_orchestrate_handler("up"))
    if command == "status":
        return Action(command, handler=_status_handler)
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
    return Action(command)


def default_runner(argv: Sequence[str], code_root: Path) -> int:
    proc = subprocess.run(list(argv), cwd=str(code_root))
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
    args = parser.parse_args(list(argv))
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

    if command in cfg.WRITE_COMMANDS:
        try:
            runtime.guard_write(
                command,
                stack_name,
                break_glass=break_glass,
                confirm_fn=confirm_fn,
                audit_fn=audit_fn,
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
