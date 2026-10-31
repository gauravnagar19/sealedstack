"""Read-only secret sources (spec 11, D-038).

sealedstack only ever *reads* secrets; nothing is written back to the source.
Each source exposes the same ``get(name) -> SecretResult`` so ``check`` can tell
three states apart (N3): found, missing, and a source error (bad token, network).
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import yaml

DEFAULT_INFRA_PROJECT = "infra"

# A process runner returns ``(returncode, stdout, stderr)``.
ProcessRunner = Callable[[Sequence[str]], "tuple[int, str, str]"]


@dataclass
class SecretResult:
    name: str
    status: str  # "found" | "missing" | "error"
    value: str = ""
    detail: str = ""

    @property
    def found(self) -> bool:
        return self.status == "found"

    @property
    def missing(self) -> bool:
        return self.status == "missing"

    @property
    def errored(self) -> bool:
        return self.status == "error"


class SecretSource:
    kind = "unknown"

    def get(self, name: str) -> SecretResult:  # pragma: no cover - interface
        raise NotImplementedError


class EnvSource(SecretSource):
    """Environment variables; the local/test default."""

    kind = "env"

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        self.env = dict(env or {})

    def get(self, name: str) -> SecretResult:
        value = self.env.get(name)
        if value:
            return SecretResult(name, "found", value)
        return SecretResult(name, "missing", detail=f"{name} is not set in the environment")


CI_SECRETS_VAR = "SEALEDSTACK_CI_SECRETS"


def parse_ci_secrets(raw: Optional[str]) -> "tuple[dict[str, str], str]":
    """``(secrets, problem)`` from the workflow's ``toJSON(secrets)`` value.

    Missing or empty is fine (``{}``, no problem: plain environment variables are
    used). Anything that is not a JSON object is reported as a problem, never
    echoed. Non-string and empty values are dropped.
    """
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return {}, f"{CI_SECRETS_VAR} is not valid JSON"
    if not isinstance(data, dict):
        return {}, f"{CI_SECRETS_VAR} is not a JSON object"
    return {str(k): v for k, v in data.items() if isinstance(v, str) and v}, ""


class CiSource(EnvSource):
    """CI platform secrets (D-050 ``secrets.source: ci``).

    The reusable workflow forwards the caller's repository secrets to the engine
    container as ONE environment variable, ``SEALEDSTACK_CI_SECRETS``, holding
    ``toJSON(secrets)`` (GitHub only exposes the secrets a step names, so a source
    that is not named would otherwise never arrive). A lookup reads that JSON first
    and falls back to a plain environment variable of the same name. The value is
    masked in the job log and never printed by the engine.
    """

    kind = "ci"

    def __init__(self, env: Optional[Mapping[str, str]] = None) -> None:
        super().__init__(env)
        self.forwarded, self.problem = parse_ci_secrets(self.env.get(CI_SECRETS_VAR))

    def get(self, name: str) -> SecretResult:
        if name in self.forwarded:
            return SecretResult(name, "found", self.forwarded[name])
        value = self.env.get(name)
        if value:
            return SecretResult(name, "found", value)
        hint = (
            f"{self.problem}; the caller workflow must pass `secrets: inherit`"
            if self.problem
            else f"{name} is not among the secrets the workflow forwarded; the caller workflow must pass `secrets: inherit`, "
                 "and the value must be a repository secret (environment secrets are not forwarded)"
        )
        return SecretResult(name, "missing", detail=hint)


def default_process_runner(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(list(argv), capture_output=True, text=True, timeout=60)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


class BitwardenSource(SecretSource):
    """Bitwarden Secrets Manager via ``bws`` (cached once per run).

    ``bws project list`` then ``bws secret list <project-id>``; the secret list
    is fetched at most once and reused for every lookup.
    """

    kind = "bitwarden"

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        project: str = "infra",
        runner: Optional[ProcessRunner] = None,
    ) -> None:
        self.env = dict(env or {})
        self.project = project
        self.runner = runner or default_process_runner
        self._cache: Optional[dict[str, str]] = None
        self._error: str = ""

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self.env.get("BWS_ACCESS_TOKEN"):
            self._error = "BWS_ACCESS_TOKEN is not set"
            raise RuntimeError(self._error)

        rc, out, err = self.runner(["bws", "project", "list", "--output", "json"])
        if rc != 0:
            raise RuntimeError(f"bws project list failed: {err or out}")
        try:
            projects = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bws project list returned invalid JSON: {exc}") from exc

        project_id = ""
        for project in projects:
            if project.get("name") == self.project:
                project_id = project.get("id", "")
                break
        if not project_id:
            raise RuntimeError(f"Bitwarden project '{self.project}' not found")

        rc, out, err = self.runner(["bws", "secret", "list", project_id, "--output", "json"])
        if rc != 0:
            raise RuntimeError(f"bws secret list failed: {err or out}")
        try:
            secrets = json.loads(out)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"bws secret list returned invalid JSON: {exc}") from exc

        self._cache = {entry.get("key", ""): entry.get("value", "") for entry in secrets}
        return self._cache

    def get(self, name: str) -> SecretResult:
        # An exported env var always wins, so operators can override locally.
        if self.env.get(name):
            return SecretResult(name, "found", self.env[name])
        try:
            secrets = self._load()
        except RuntimeError as exc:
            return SecretResult(name, "error", detail=str(exc))
        if name in secrets:
            return SecretResult(name, "found", secrets[name])
        return SecretResult(name, "missing", detail=f"{name} not found in Bitwarden project '{self.project}'")


class SopsSource(SecretSource):
    """A SOPS-encrypted YAML file, decrypted with the key in the environment."""

    kind = "sops"

    def __init__(
        self,
        config_dir: Path,
        env: Optional[Mapping[str, str]] = None,
        file: str = "secrets/infra.enc.yaml",
        runner: Optional[ProcessRunner] = None,
    ) -> None:
        self.env = dict(env or {})
        self.path = Path(config_dir) / file
        self.runner = runner or default_process_runner
        self._cache: Optional[dict[str, str]] = None

    def _load(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self.path.exists():
            raise RuntimeError(f"SOPS file not found: {self.path}")
        rc, out, err = self.runner(
            ["sops", "--decrypt", "--input-type", "yaml", "--output-type", "yaml", str(self.path)]
        )
        if rc != 0:
            raise RuntimeError(f"sops decrypt failed: {err or out}")
        data = yaml.safe_load(out) or {}
        self._cache = {str(k): "" if v is None else str(v) for k, v in data.items()}
        return self._cache

    def get(self, name: str) -> SecretResult:
        if self.env.get(name):
            return SecretResult(name, "found", self.env[name])
        try:
            secrets = self._load()
        except RuntimeError as exc:
            return SecretResult(name, "error", detail=str(exc))
        if name in secrets:
            return SecretResult(name, "found", secrets[name])
        return SecretResult(name, "missing", detail=f"{name} not found in {self.path.name}")


CLI_BACKENDS = ("aws_ssm", "vault", "openbao", "onepassword", "pass", "gcp", "azure_kv")


class CliSource(SecretSource):
    """Read secrets through a provider CLI (aws_ssm, vault/openbao, onepassword, pass).
    gcp/azure_kv have no CLI here and error. (Doppler and Infisical are read natively
    over their APIs, ``doppler.DopplerSource`` and ``infisical.InfisicalSource``.)"""

    kind = "cli"

    def __init__(
        self,
        backend: str,
        stack: dict,
        config_dir: Path,
        env: Optional[Mapping[str, str]] = None,
        *,
        runner: Optional[ProcessRunner] = None,
        project: Optional[str] = None,
    ) -> None:
        self.backend = backend
        self.stack = stack
        self.config_dir = Path(config_dir)
        self.env = dict(env or {})
        self.runner = runner
        cfg = (stack.get("providers") or {}).get(f"secrets_{backend}") or {}
        self.project = str(project or (stack.get("secrets") or {}).get("project") or cfg.get("project") or "infra")

    def _cfg(self, key: str) -> str:
        file = self.config_dir / "providers" / f"secrets_{self.backend}.yaml"
        value = None
        if file.exists():
            try:
                value = (yaml.safe_load(file.read_text(encoding="utf-8")) or {}).get(key)
            except yaml.YAMLError:
                value = None
        if value in (None, ""):
            value = ((self.stack.get("providers") or {}).get(f"secrets_{self.backend}") or {}).get(key)
        return "" if value is None else str(value)

    def _run(self, argv: Sequence[str], extra_env: Optional[Mapping[str, str]] = None) -> tuple[int, str, str]:
        if self.runner is not None:
            return self.runner(list(argv))
        import os

        env = {**os.environ, **self.env, **(extra_env or {})}
        try:
            proc = subprocess.run(list(argv), capture_output=True, text=True, env=env, timeout=60)
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()

    def _read(self, name: str) -> tuple[str, str, str]:
        backend = self.backend
        if backend == "aws_ssm":
            rc, out, _ = self._run(
                ["aws", "ssm", "get-parameter", "--with-decryption", "--name",
                 f"/sealedstack/{self.project}/{name}", "--query", "Parameter.Value", "--output", "text"]
            )
            return ("found", out, "") if rc == 0 and out else ("missing", "", "")
        if backend in ("vault", "openbao"):
            mount = self._cfg("mount") or "secret"
            prefix = self._cfg("prefix")
            addr = self._cfg("addr") or self.env.get("VAULT_ADDR") or self.env.get("OPENBAO_ADDR", "")
            token_name = "OPENBAO_TOKEN" if backend == "openbao" else "VAULT_TOKEN"
            token = self.env.get(token_name, "")
            if not addr or not token:
                return ("error", "", f"secrets backend '{backend}' read needs an address and token")
            path = f"{prefix.rstrip('/')}/{name}" if prefix else name
            rc, out, _ = self._run(
                ["vault", "kv", "get", f"-mount={mount}", "-field=value", path],
                {"VAULT_ADDR": addr, "VAULT_TOKEN": token},
            )
            return ("found", out, "") if rc == 0 and out else ("missing", "", "")
        if backend == "onepassword":
            rc, out, _ = self._run(
                ["op", "read", f"op://{self._cfg('vault')}/{name}/password"],
                {"OP_SERVICE_ACCOUNT_TOKEN": self.env.get("OP_SERVICE_ACCOUNT_TOKEN", "")},
            )
            return ("found", out, "") if rc == 0 and out else ("missing", "", "")
        if backend == "pass":
            prefix = self._cfg("subdir")
            path = f"{prefix}/{name}" if prefix else name
            rc, out, _ = self._run(["pass", "show", path], {"PASSWORD_STORE_DIR": self._cfg("store_dir")})
            first = out.splitlines()[0] if out else ""
            return ("found", first, "") if rc == 0 and first else ("missing", "", "")
        return ("error", "", f"read not supported for secrets backend '{backend}'")

    def get(self, name: str) -> SecretResult:
        if self.env.get(name):
            return SecretResult(name, "found", self.env[name])
        try:
            status, value, detail = self._read(name)
        except Exception as exc:  # noqa: BLE001
            return SecretResult(name, "error", detail=str(exc))
        return SecretResult(name, status, value, detail)


def build_source(
    stack: dict,
    config_dir: Path,
    env: Optional[Mapping[str, str]] = None,
    *,
    project: Optional[str] = None,
    bw_runner: Optional[ProcessRunner] = None,
    sops_runner: Optional[ProcessRunner] = None,
    cli_runner: Optional[ProcessRunner] = None,
    http=None,
) -> SecretSource:
    """Pick the source named by ``slots.secrets`` (default ``env``).

    The project is, in order: the ``project`` argument (app secrets pass
    ``secrets.app_project``), ``stack.yaml: secrets.project`` (D-050, default
    ``infra``), the legacy ``providers.secrets_bitwarden.project_name``. Doppler has no
    such project: its service token is scoped to one config, and a second config for the
    app secrets uses ``DOPPLER_TOKEN_APPS``. Infisical names its project in
    ``providers.secrets_infisical`` and keeps app secrets in ``apps_secret_path``.
    """
    env = dict(env or {})
    kind = (stack.get("slots") or {}).get("secrets") or "env"
    if kind == "bitwarden":
        cfg = (stack.get("providers") or {}).get("secrets_bitwarden") or {}
        project = project or (stack.get("secrets") or {}).get("project") or cfg.get("project_name") or "infra"
        return BitwardenSource(env, project=str(project), runner=bw_runner)
    if kind == "sops":
        cfg = (stack.get("providers") or {}).get("secrets_sops") or {}
        return SopsSource(config_dir, env, file=cfg.get("file", "secrets/infra.enc.yaml"), runner=sops_runner)
    if kind == "doppler":
        # App secrets (the `project` argument is the app project) may live in a second
        # config with its own service token; infra secrets use DOPPLER_TOKEN.
        from .doppler import DopplerSource

        infra = str((stack.get("secrets") or {}).get("project") or DEFAULT_INFRA_PROJECT)
        return DopplerSource(env, apps=project is not None and str(project) != infra, http=http)
    if kind == "infisical":
        # Same split as Doppler: the app secrets may live in their own folder
        # (`apps_secret_path`); the project, environment and site are plain config.
        from .infisical import InfisicalSource, load_settings

        infra = str((stack.get("secrets") or {}).get("project") or DEFAULT_INFRA_PROJECT)
        return InfisicalSource(
            env, load_settings(stack, config_dir), apps=project is not None and str(project) != infra, http=http
        )
    if kind in CLI_BACKENDS:
        return CliSource(kind, stack, config_dir, env, runner=cli_runner, project=project)
    if kind == "ci":
        return CiSource(env)
    return EnvSource(env)


_WARNED: set[str] = set()


def lookup(source: SecretSource, config, logical: str) -> SecretResult:
    """Read a logical secret through the config's key mapping (D-050).

    The configured (or default) key wins. If it is absent and the mapping names no
    other key, a deprecated older key (``state_passphrase``) is still honoured with
    a one-time warning, so existing setups keep working while they rename it.
    """
    key = config.secret_key(logical)
    result = source.get(key)
    if result.found:
        return result
    legacy = config.legacy_secret_key(logical)
    if legacy and legacy != key:
        old = source.get(legacy)
        if old.found:
            if legacy not in _WARNED:
                _WARNED.add(legacy)
                import sys

                print(
                    f"sealedstack: warning: reading the deprecated secret key '{legacy}' for "
                    f"`{logical}`; rename it to '{key}' (or set secrets.keys.{logical}: {legacy} in "
                    "stack.yaml). The fallback will be removed in a later release.",
                    file=sys.stderr,
                )
            return SecretResult(legacy, "found", old.value, detail=f"deprecated key '{legacy}'")
    return result


def _infisical_settings(config):
    from .infisical import load_settings

    return load_settings(config.stack, config.config_dir)


def scope(config) -> str:
    """Where in the source the infra secrets live, for messages.

    Most sources have a named project; a Doppler service token is scoped to one config
    by Doppler itself, so there is nothing to name; Infisical is named by project, environment
    and folder."""
    kind = str((config.stack.get("slots") or {}).get("secrets") or "")
    if kind == "doppler":
        return "the config of the Doppler service token"
    if kind == "infisical":
        return _infisical_settings(config).describe()
    return f"project '{config.source_project()}'"


def where(config) -> str:
    """``Bitwarden, project `infra``` / ``Doppler, the config of the service token``."""
    kind = str((config.stack.get("slots") or {}).get("secrets") or "bitwarden")
    if kind == "doppler":
        return "Doppler, the config of your service token"
    if kind == "infisical":
        return _infisical_settings(config).describe()
    return f"{kind.capitalize()}, project `{config.source_project()}`"


def describe(config, logical: str) -> str:
    """``logical`` (source key ``KEY``) -- how every message names a secret (D-050)."""
    key = config.secret_key(logical)
    name = str(logical).lower()
    return f"{name} (source key {key})"
