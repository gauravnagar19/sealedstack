"""Read platform stack outputs (D-047).

``platform_ready`` / ``platform_output`` (from the old ``scripts/db.sh``): init the platform
stack once, then read a named output. Used by env-sync, deploy-apps and db.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod

# A capture runner returns (returncode, stdout).
CaptureRunner = Callable[[Sequence[str], Mapping[str, str]], "tuple[int, str]"]


def _default_capture(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    result = subprocess.run(list(argv), env=dict(env), capture_output=True, text=True)
    return result.returncode, result.stdout


class PlatformState:
    """Lazily ``tofu init``-ed access to the platform stack's outputs."""

    def __init__(
        self,
        config: config_mod.Config,
        env: Optional[Mapping[str, str]] = None,
        source: Optional[secrets_mod.SecretSource] = None,
        *,
        capture: Optional[CaptureRunner] = None,
        init_runner: Optional[tofu_mod.TofuRunner] = None,
    ) -> None:
        self.config = config
        self.env = dict(env or {})
        self.source = source or secrets_mod.build_source(config.stack, config.config_dir, self.env)
        self.platform_dir = Path(config.code_root) / "tofu" / "stacks" / "platform"
        self._capture = capture or _default_capture
        self._init_runner = init_runner
        self._child_env: Optional[dict] = None

    def _ensure(self) -> dict:
        if self._child_env is None:
            passphrase = tofu_mod.ready(
                self.config, self.env, self.source, self.platform_dir, runner=self._init_runner
            )
            self._child_env = {**self.env, "TF_VAR_state_passphrase": passphrase}
        return self._child_env

    def output(self, name: str) -> Any:
        child_env = self._ensure()
        rc, out = self._capture(
            ["tofu", "-chdir", str(self.platform_dir), "output", "-json", name], child_env
        )
        if rc != 0:
            raise config_mod.ConfigError(f"tofu output {name} failed (exit {rc})")
        return json.loads(out or "null")
