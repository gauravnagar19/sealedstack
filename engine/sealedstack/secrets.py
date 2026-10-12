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


class CiSource(EnvSource):
    """CI platform secrets, delivered to the engine as environment variables."""

    kind = "ci"


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


CLI_BACKENDS = ("aws_ssm", "vault", "openbao", "infisical", "doppler", "onepassword", "pass", "gcp", "azure_kv")


class CliSource(SecretSource):
    """Read secrets through a provider CLI (aws_ssm, vault/openbao, infisical,
    doppler, onepassword, pass). gcp/azure_kv have no CLI here and error."""

    kind = "cli"

    def __init__(
        self,
        backend: str,
        stack: dict,
        config_dir: Path,
        env: Optional[Mapping[str, str]] = None,
        *,
        runner: Optional[ProcessRunner] = None,
    ) -> None:
        self.backend = backend
        self.stack = stack
        self.config_dir = Path(config_dir)
        self.env = dict(env or {})
        self.runner = runner
        cfg = (stack.get("providers") or {}).get(f"secrets_{backend}") or {}
        self.project = str(cfg.get("project") or "infra")

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
        if backend == "infisical":
            rc, out, _ = self._run(
                ["infisical", "secrets", "get", name, "--projectId", self._cfg("project_id"),
                 "--env", self._cfg("environment") or "prod", "--plain"],
                {"INFISICAL_TOKEN": self.env.get("INFISICAL_TOKEN", "")},
            )
            return ("found", out, "") if rc == 0 and out else ("missing", "", "")
        if backend == "doppler":
            rc, out, _ = self._run(
                ["doppler", "secrets", "get", name, "--project", self._cfg("project"),
                 "--config", self._cfg("config"), "--plain"],
                {"DOPPLER_TOKEN": self.env.get("DOPPLER_TOKEN", "")},
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
) -> SecretSource:
    """Pick the source named by ``slots.secrets`` (default ``env``).

    ``project`` overrides the Bitwarden project name (infra secrets live in the
    default ``infra`` project; app secrets in ``prod``).
    """
    env = dict(env or {})
    kind = (stack.get("slots") or {}).get("secrets") or "env"
    if kind == "bitwarden":
        cfg = (stack.get("providers") or {}).get("secrets_bitwarden") or {}
        project = project or cfg.get("project_name", "infra")
        return BitwardenSource(env, project=project, runner=bw_runner)
    if kind == "sops":
        cfg = (stack.get("providers") or {}).get("secrets_sops") or {}
        return SopsSource(config_dir, env, file=cfg.get("file", "secrets/infra.enc.yaml"), runner=sops_runner)
    if kind in CLI_BACKENDS:
        return CliSource(kind, stack, config_dir, env, runner=cli_runner)
    if kind == "ci":
        return CiSource(env)
    return EnvSource(env)
