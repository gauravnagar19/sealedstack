"""Values sealedstack generates, kept only in the encrypted state (D-038, D-044).

The ``generated`` OpenTofu stack holds the Dokploy admin password, the Dokploy ->
worker SSH key pair, the CI SSH key pair and a small map of run-time values (the
Dokploy API key, created through Dokploy's API on first boot). Nothing here is
ever written to the secret source or to git. ``Generated`` is the one reader and
writer; consumers (server bootstrap, platform, the Dokploy client) ask it.

The stack is the only place that creates the key pairs and the password, so they
are stable across runs; the run-time map is read, merged and passed back whole
(see ``tofu/stacks/generated/variables.tf``).
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

from . import config as config_mod
from . import secrets as secrets_mod
from . import tofu as tofu_mod

OUTPUT_KEYS = (
    "dokploy_admin_password",
    "dokploy_ssh_public_key",
    "dokploy_ssh_private_key",
    "ci_ssh_public_key",
    "ci_ssh_private_key",
)


class GeneratedError(config_mod.ConfigError):
    pass


class TofuBackend:
    """The real stack, driven through the ``tofu`` CLI against the R2 backend."""

    def __init__(
        self,
        config: config_mod.Config,
        env: Mapping[str, str],
        source: secrets_mod.SecretSource,
        *,
        http=None,
        run: Optional[Callable[[list, Mapping[str, str]], "tuple[int, str]"]] = None,
    ) -> None:
        self.config, self.env, self.source, self.http = config, dict(env), source, http
        self.dir = Path(config.code_root) / "tofu" / "stacks" / "generated"
        self._run = run or self._default_run
        self._passphrase = ""

    @staticmethod
    def _default_run(argv, env) -> "tuple[int, str]":
        proc = subprocess.run(list(argv), env=dict(env), capture_output=True, text=True)
        return proc.returncode, proc.stdout

    def _env(self, extra: Optional[Mapping[str, str]] = None) -> dict:
        env = {**self.env, "TF_VAR_state_passphrase": self._passphrase, "TF_IN_AUTOMATION": "1"}
        env.update(extra or {})
        return env

    def init(self) -> None:
        def runner(argv, env):
            rc, _ = self._run(list(argv), env)
            return rc

        self._passphrase = tofu_mod.ready(self.config, self.env, self.source, self.dir, runner=runner, http=self.http)

    def outputs(self) -> dict:
        rc, out = self._run(["tofu", f"-chdir={self.dir}", "output", "-json"], self._env())
        if rc != 0 or not out.strip():
            return {}
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            return {}
        return {name: spec.get("value") for name, spec in data.items() if isinstance(spec, dict)}

    def apply(self, values: Mapping[str, str]) -> None:
        rc, out = self._run(
            ["tofu", f"-chdir={self.dir}", "apply", "-auto-approve", "-input=false", "-no-color"],
            self._env({"TF_VAR_values": json.dumps(dict(values))}),
        )
        if rc != 0:
            raise GeneratedError(f"could not update the generated state (tofu apply exit {rc})")


class Generated:
    def __init__(self, backend) -> None:
        self.backend = backend
        self._outputs: Optional[dict] = None
        self._inited = False

    @classmethod
    def for_config(cls, config, env, source, *, http=None, run=None) -> "Generated":
        return cls(TofuBackend(config, env, source, http=http, run=run))

    def _init(self) -> None:
        if not self._inited:
            self.backend.init()
            self._inited = True

    def read(self, *, refresh: bool = False) -> dict:
        """The current outputs (empty before the stack was ever applied)."""
        if self._outputs is None or refresh:
            self._init()
            self._outputs = self.backend.outputs()
        return self._outputs

    def ensure(self) -> dict:
        """Create what is missing (keys, admin password) and return the outputs."""
        current = self.read()
        if all(current.get(key) for key in OUTPUT_KEYS):
            return current
        self.backend.apply(dict(current.get("values") or {}))
        out = self.read(refresh=True)
        missing = [key for key in OUTPUT_KEYS if not out.get(key)]
        if missing:
            raise GeneratedError("the generated state is incomplete after apply: " + ", ".join(missing))
        return out

    def values(self) -> dict:
        return dict(self.read().get("values") or {})

    def store(self, **updates: str) -> None:
        """Merge ``updates`` into the stored run-time values (and keep the rest)."""
        out = self.ensure()
        merged = {**(out.get("values") or {}), **{k: v for k, v in updates.items() if v is not None}}
        self.backend.apply(merged)
        self.read(refresh=True)

    # -- convenience --------------------------------------------------------
    def get(self, key: str) -> str:
        return str(self.read().get(key) or "")

    def api_key(self) -> str:
        return str(self.values().get("dokploy_api_key") or "")


_CACHE: dict = {}


def shared(config, env, source, *, http=None) -> Generated:
    """One ``Generated`` per config dir per process (so a phase initialises tofu once)."""
    key = str(config.config_dir)
    if key not in _CACHE:
        _CACHE[key] = Generated.for_config(config, env, source, http=http)
    return _CACHE[key]


def resolve_api_key(config, env, source, generated: Optional[Generated] = None) -> str:
    """The Dokploy API key: an explicit ``dokploy_api_key`` in the secret source wins
    (the manual path), else the one created on first boot and kept in the state."""
    result = secrets_mod.lookup(source, config, "DOKPLOY_API_KEY")
    if result.found:
        return result.value
    return (generated or shared(config, env, source)).api_key()


def show_admin(config, env, source, *, generated: Optional[Generated] = None, out=None, is_ci: bool = False,
               break_glass: bool = False) -> int:
    """Print the Dokploy admin login once, for a local break-glass session only.

    Refused in CI (a log line is the one place this must never land) and without
    ``--break-glass``. The first successful call records ``admin_shown`` in the state;
    later calls refuse unless ``--again`` semantics are added by the caller.
    """
    import sys

    out = out if out is not None else sys.stdout
    if is_ci:
        print("sealedstack: --show-dokploy-admin is refused in CI: the password must never reach a job log.", file=sys.stderr)
        return 2
    if not break_glass:
        print("sealedstack: --show-dokploy-admin needs --break-glass (a local, guarded session).", file=sys.stderr)
        return 2
    kept = secrets_mod.lookup(source, config, "dokploy_admin_password")
    if kept.found:
        print(f"Dokploy admin: {config.stack.get('owner_email', '')}", file=out)
        print(f"The password is the one you keep in the secret source: {secrets_mod.describe(config, 'dokploy_admin_password')}.", file=out)
        return 0
    gen = generated or shared(config, env, source)
    state = gen.ensure()
    if gen.values().get("admin_shown"):
        print("sealedstack: the Dokploy admin login was already shown once; it is not shown again. Reset it in "
              "the Dokploy panel (or rotate the generated state) if you lost it.", file=sys.stderr)
        return 1
    print(f"Dokploy admin: {config.stack.get('owner_email', '')}", file=out)
    print(f"Password:      {state['dokploy_admin_password']}", file=out)
    print("(shown once; store it in your password manager now)", file=sys.stderr)
    gen.store(admin_shown="true")
    return 0


def main(argv=None) -> int:
    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        source = secrets_mod.build_source(config.stack, config.config_dir, env)
        shared(config, env, source).ensure()
    except config_mod.ConfigError as exc:
        import sys

        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1
    print("generated: keys and passwords are in the encrypted state")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
