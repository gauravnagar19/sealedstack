"""Config loading, code-root discovery and the local-run rules (spec 6.6, D-045).

This module holds everything the CLI needs to know *where* it is running and
*what it is allowed to do there*:

* ``find_code_root`` / ``resolve_config_dir`` locate the engine code and the
  user's config directory (``--config`` > ``$SEALEDSTACK_CONFIG`` > ``<repo>/config``).
* ``load_config`` reads the three user config files.
* ``Runtime`` detects CI from the adapter's environment marker and enforces the
  local break-glass rules for write commands.

Everything is injectable (env mapping and a git runner) so the rules are unit
testable without touching a real repository or process environment.
"""
from __future__ import annotations

import os
import subprocess
import sys
import atexit
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import yaml

# Presence of any of these marks a CI-adapter run. The engine only ever reads
# one; the adapters set different variables.
CI_MARKERS = ("GITHUB_ACTIONS", "GITLAB_CI", "SEALEDSTACK_CI")

# Commands that change infrastructure or data. Locally they are refused unless
# --break-glass is given (spec 6.6, D-045). `db drill` / `backup check` are read-only
# (throwaway database) and `backup list` only reads, so they are deliberately absent.
WRITE_COMMANDS = frozenset(
    {
        "apply",
        "up",
        "server add",
        "server sync",
        "server reinstall",
        "db restore",
        "backup restore",  # D-085: the same restore under the `backup` noun
    }
)


# D-050: the logical secrets every setup writes explicitly into
# ``stack.yaml: secrets.keys`` (logical name -> default key in the secret source).
# Any other logical name defaults to its upper-cased form (``hcloud_token`` ->
# ``HCLOUD_TOKEN``), so renaming is always a one-line change in config.
CORE_SECRET_KEYS = {
    "cloudflare_api_token": "CLOUDFLARE_API_TOKEN",
    "tailscale_oauth_client_id": "TAILSCALE_OAUTH_CLIENT_ID",
    "tailscale_oauth_client_secret": "TAILSCALE_OAUTH_CLIENT_SECRET",
    "state_passphrase": "STATE_PASSPHRASE",
    "bot_private_key": "SEALEDSTACK_BOT_PRIVATE_KEY",
}

# Source keys older releases read; still honoured (with a warning) when the
# default key is absent and the mapping does not name another key.
LEGACY_SECRET_KEYS = {
    "state_passphrase": "state_passphrase",
}

DEFAULT_SECRET_PROJECT = "infra"
DEFAULT_APP_SECRET_PROJECT = "prod"


def default_secret_key(logical: str) -> str:
    """The key a logical secret has in the source unless config renames it."""
    logical = str(logical)
    return CORE_SECRET_KEYS.get(logical.lower(), logical.upper())


def default_secret_keys() -> dict:
    """The explicit defaults ``setup`` writes into ``stack.yaml`` (D-050)."""
    return dict(CORE_SECRET_KEYS)


def default_bootstrap_secrets(server: str) -> tuple[str, str]:
    """(ip_secret, password_secret) defaults for an existing server (D-049)."""
    env_name = str(server).upper().replace("-", "_")
    return f"SERVER_IP_{env_name}", f"ROOT_PASSWORD_{env_name}"


class ConfigError(RuntimeError):
    """A problem locating or reading the config/code root."""


class BreakGlassError(RuntimeError):
    """A local write command was refused (default or a failed guard)."""


def find_code_root(env: Mapping[str, str] | None = None) -> Path:
    """Locate the directory that holds ``scripts/``, ``schemas/`` and ``VERSION``.

    ``SEALEDSTACK_CODE`` wins (the image sets it); otherwise we walk up from this
    file, which works for editable installs and repo checkouts alike.
    """
    env = os.environ if env is None else env
    override = env.get("SEALEDSTACK_CODE")
    if override and (Path(override) / "scripts").is_dir():
        return Path(override)

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "VERSION").is_file() and (parent / "scripts" / "validate.py").is_file():
            return parent
    raise ConfigError(
        "could not locate the sealedstack code root "
        "(expected a directory with VERSION and scripts/validate.py; set SEALEDSTACK_CODE)"
    )


def resolve_config_dir(
    config_arg: str | None = None,
    env: Mapping[str, str] | None = None,
    code_root: Path | None = None,
) -> Path:
    """The config directory: ``--config`` > ``$SEALEDSTACK_CONFIG`` > ``<repo>/config``."""
    env = os.environ if env is None else env
    if config_arg:
        return Path(config_arg).expanduser()
    override = env.get("SEALEDSTACK_CONFIG")
    if override:
        return Path(override).expanduser()
    code_root = code_root or find_code_root(env)
    return code_root / "config"


def load_yaml(path: Path) -> dict:
    """Load a YAML file, returning an empty mapping when it is empty/missing."""
    if not Path(path).exists():
        return {}
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def read_version(code_root: Path) -> str:
    version_file = Path(code_root) / "VERSION"
    if not version_file.is_file():
        return ""
    return version_file.read_text(encoding="utf-8").strip()


@dataclass
class Config:
    """The resolved user config (no secrets, D-008)."""

    code_root: Path
    config_dir: Path
    stack: dict
    servers: dict
    apps: dict

    @property
    def name(self) -> str:
        return str(self.stack.get("name") or "")

    @property
    def state_prefix(self) -> str:
        """The R2 namespace of state, lock, progress and drill results: ``state.prefix``, else ``name``.

        Set it to keep existing state when the infra repo is renamed."""
        return str((self.stack.get("state") or {}).get("prefix") or self.name)

    @property
    def domain(self) -> str:
        return str(self.stack.get("domain") or "")

    def admin_ssh_keys(self) -> list[str]:
        """Operator SSH public keys from config/admin.ssh_keys (D-040)."""
        keys = (self.stack.get("admin") or {}).get("ssh_keys") or []
        return [str(key) for key in keys]

    # -- secret key mapping (D-050) -----------------------------------------

    def source_project(self, default: str = DEFAULT_SECRET_PROJECT) -> str:
        """The secret-source project for infra secrets (``secrets.project``)."""
        return str((self.stack.get("secrets") or {}).get("project") or default)

    def app_project(self, default: str = DEFAULT_APP_SECRET_PROJECT) -> str:
        """The project that holds app secrets (``secrets.app_project``, default ``prod``)."""
        return str((self.stack.get("secrets") or {}).get("app_project") or default)

    def explicit_secret_key(self, logical: str) -> str:
        """The key named in ``secrets.keys`` for a logical secret, or ``""``.

        Logical names are matched case-insensitively, so configs written with the
        older upper-case names keep working.
        """
        keys = (self.stack.get("secrets") or {}).get("keys") or {}
        wanted = str(logical).lower()
        for name, value in keys.items():
            if str(name).lower() == wanted and value:
                return str(value)
        return ""

    def secret_key(self, logical: str) -> str:
        """The source key for a logical secret: the configured one, else the default."""
        return self.explicit_secret_key(logical) or default_secret_key(logical)

    def legacy_secret_key(self, logical: str) -> str:
        """A deprecated fallback key, only when config does not rename the secret."""
        if self.explicit_secret_key(logical):
            return ""
        return LEGACY_SECRET_KEYS.get(str(logical).lower(), "")

    def bootstrap_secrets(self, name: str) -> tuple[str, str]:
        """(ip_secret, password_secret) for an existing server (D-049 defaults)."""
        spec = (self.servers.get("servers") or {}).get(name) or {}
        boot = spec.get("bootstrap") or {}
        default_ip, default_pw = default_bootstrap_secrets(name)
        return (
            str(boot.get("ip_secret") or default_ip),
            str(boot.get("password_secret") or default_pw),
        )

    def app_secret_key(self, app: str, name: str) -> str:
        """The source key for one app secret (default ``<app>.<NAME>``)."""
        spec = (self.apps.get("apps") or {}).get(app) or {}
        return str((spec.get("secrets") or {}).get(name, f"{app}.{name}"))

    # -- approval (D-052) ---------------------------------------------------

    def approvers(self) -> list[str]:
        """GitHub usernames allowed to approve write commands in CI (D-052)."""
        return [str(name) for name in (self.stack.get("approvers") or [])]


def load_config(
    config_dir: Path | None = None,
    code_root: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    env = os.environ if env is None else env
    code_root = Path(code_root) if code_root else find_code_root(env)
    config_dir = Path(config_dir) if config_dir else resolve_config_dir(env=env, code_root=code_root)
    return Config(
        code_root=code_root,
        config_dir=config_dir,
        stack=load_yaml(config_dir / "stack.yaml"),
        servers=load_yaml(config_dir / "servers.yaml"),
        apps=load_yaml(config_dir / "apps.yaml"),
    )


@dataclass
class GitInfo:
    clean: bool
    head: str
    origin_main: str

    @property
    def matches_origin_main(self) -> bool:
        return bool(self.head) and self.head == self.origin_main


# A git runner takes a git argv (without the leading ``git``) and returns
# ``(returncode, stdout, stderr)``. Injecting it keeps the guard testable.
GitRunner = Callable[[Sequence[str]], "tuple[int, str, str]"]


def default_git_runner(code_root: Path) -> GitRunner:
    def run(args: Sequence[str]) -> tuple[int, str, str]:
        try:
            proc = subprocess.run(
                ["git", *args],
                cwd=str(code_root),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 - no git / not a repo: report as failure
            return 1, "", str(exc)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    return run


def inspect_git(git: GitRunner) -> GitInfo:
    """Best-effort cleanliness + ``HEAD == origin/main`` check (spec 6.6)."""
    rc_status, status, _ = git(["status", "--porcelain"])
    rc_head, head, _ = git(["rev-parse", "HEAD"])
    rc_main, origin, _ = git(["rev-parse", "origin/main"])
    return GitInfo(
        clean=(rc_status == 0 and status == ""),
        head=head if rc_head == 0 else "",
        origin_main=origin if rc_main == 0 else "",
    )


def audit_line(
    command: str,
    info: GitInfo,
    stack_name: str,
    env: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """The audit record posted/logged for a break-glass run (spec 6.6)."""
    env = os.environ if env is None else env
    who = env.get("GITHUB_ACTOR") or env.get("GITLAB_USER_LOGIN") or env.get("USER") or "unknown"
    when = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"sealedstack audit: user={who} at={when} command='{command}' "
        f"name={stack_name} commit={info.head or '?'} ref=origin/main break-glass"
    )


@dataclass
class Runtime:
    """Where the engine is running, and what that permits (D-045)."""

    code_root: Path
    env: Mapping[str, str]
    git: GitRunner

    @classmethod
    def detect(
        cls,
        code_root: Path,
        env: Mapping[str, str] | None = None,
        git: GitRunner | None = None,
    ) -> "Runtime":
        env = os.environ if env is None else env
        return cls(code_root=Path(code_root), env=env, git=git or default_git_runner(Path(code_root)))

    @property
    def is_ci(self) -> bool:
        return any(self.env.get(marker) for marker in CI_MARKERS)

    def guard_write(
        self,
        command: str,
        stack_name: str,
        *,
        break_glass: bool = False,
        confirm_fn: Callable[[str], str] | None = None,
        audit_fn: Callable[[str], None] | None = None,
        forge=None,
        repo: str = "",
        lock=None,
    ) -> None:
        """Refuse a local write, or run the break-glass guards (spec 6.6)."""
        if command not in WRITE_COMMANDS or self.is_ci:
            return

        if not break_glass:
            raise BreakGlassError(
                f"{command}: refused on a local runner. Infrastructure changes come from "
                "merged main via CI; re-run with --break-glass only in an emergency (spec 6.6)."
            )

        info = inspect_git(self.git)
        if not info.clean:
            raise BreakGlassError(
                f"{command}: --break-glass requires a clean working tree "
                "(git status --porcelain is not empty)."
            )
        if not info.matches_origin_main:
            raise BreakGlassError(
                f"{command}: --break-glass requires HEAD == origin/main "
                f"(HEAD={info.head or '?'}, origin/main={info.origin_main or 'unavailable'})."
            )

        # Condition 2 (spec 6.6): take the state lock so CI cannot run at the
        # same time. Prefer the remote R2 lock (shared with CI); fall back to a
        # local file lock when no R2 credentials are available.
        if lock is not None:
            from .lock import LockUnavailable

            try:
                info = lock.acquire()
            except LockUnavailable:
                lock = None  # no state bucket yet (before the first `up`): use the local file lock below
            except Exception as exc:  # noqa: BLE001 - LockHeld
                raise BreakGlassError(f"{command}: {exc} Wait for the other run to finish first.") from exc
            else:
                atexit.register(lambda: lock.release(info))
        if lock is None:
            workdir = Path(self.env.get("SEALEDSTACK_WORKDIR") or (self.code_root / ".sealedstack"))
            workdir.mkdir(parents=True, exist_ok=True)
            lock_file = workdir / "state.lock"
            try:
                fd = os.open(str(lock_file), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                # Re-entrant within one process (the engine is one command per
                # process); a different pid means a concurrent run.
                try:
                    held = lock_file.read_text(encoding="utf-8")
                except OSError:
                    held = ""
                if f"pid={os.getpid()} " in held:
                    pass
                else:
                    raise BreakGlassError(
                        f"{command}: another sealedstack run holds the state lock ({lock_file}); "
                        "wait for it to finish, or remove the file if it is stale."
                    ) from exc
            else:
                os.write(fd, f"pid={os.getpid()} command={command}\n".encode("utf-8"))
                os.close(fd)
                atexit.register(lambda: lock_file.unlink(missing_ok=True))

        if not stack_name:
            raise BreakGlassError(
                f"{command}: --break-glass requires stack.yaml:name to confirm; it is not set."
            )

        confirm = confirm_fn or input
        answer = confirm(f"Type the infra name to confirm ({stack_name}): ")
        if (answer or "").strip() != stack_name:
            raise BreakGlassError(
                f"{command}: typed name did not match '{stack_name}'; aborting."
            )

        line = audit_line(command, info, stack_name, self.env)
        if forge is not None and repo:
            try:
                from . import statusissue

                statusissue.post_problem(forge, repo, line)
                return
            except Exception as exc:  # noqa: BLE001 - fall back, but say so
                print(
                    f"sealedstack: could not post the audit to the Status issue ({exc}); "
                    "falling back to stderr",
                    file=sys.stderr,
                )
        (audit_fn or (lambda text: print(text, file=sys.stderr)))(line)
