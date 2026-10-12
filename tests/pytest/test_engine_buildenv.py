"""buildenv + userauth ports (Task 4.9 / D-047)."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from sealedstack import buildenv, config as config_mod, envfile, secrets as secrets_mod, userauth
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


def make_config(tmp_path: Path, apps: dict | None = None, stack: dict | None = None) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "domain": "example.com",
                "slots": {"secrets": "bitwarden", "user_auth": "oidc"},
                "providers": stack or {},
            }
        )
    )
    (cfg_dir / "apps.yaml").write_text(
        yaml.safe_dump({"apps": {name: {"user_auth": entry} for name, entry in (apps or {}).items()}})
    )
    (cfg_dir / "servers.yaml").write_text("servers: {}\n")
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


def write_app(tmp_path: Path, app: str, secrets: list[str], hostnames: list[str] | None = None) -> None:
    d = tmp_path / ".build" / "apps"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{app}.json").write_text(
        json.dumps(
            {
                "name": app,
                "hostnames": hostnames or [f"{app}.example.com"],
                "manifest": {"name": app, "secrets": secrets},
            }
        )
    )


# --------------------------------------------------------------------------- #
# build-env
# --------------------------------------------------------------------------- #
def test_buildenv_resolves_app_secrets(tmp_path):
    config = make_config(tmp_path)
    write_app(tmp_path, "shop", ["STRIPE_SECRET", "SESSION_KEY"])
    source = FakeSource({"shop.STRIPE_SECRET": "sk_live", "shop.SESSION_KEY": "abc"})

    data, count = buildenv.build(config, {}, source=source)

    assert count == 1
    assert data["shop"] == {"STRIPE_SECRET": "sk_live", "SESSION_KEY": "abc"}
    envfile.save(envfile.env_path(tmp_path), data)
    assert json.loads(envfile.env_path(tmp_path).read_text())["shop"]["SESSION_KEY"] == "abc"


def test_buildenv_missing_secret_is_fatal(tmp_path):
    config = make_config(tmp_path)
    write_app(tmp_path, "shop", ["STRIPE_SECRET"])
    with pytest.raises(buildenv.BuildEnvError) as exc:
        buildenv.build(config, {}, source=FakeSource({}))
    assert "shop.STRIPE_SECRET" in str(exc.value)


def test_buildenv_skips_apps_without_secrets(tmp_path):
    config = make_config(tmp_path)
    write_app(tmp_path, "landing", [])
    data, count = buildenv.build(config, {}, source=FakeSource({}))
    assert data == {} and count == 0


# --------------------------------------------------------------------------- #
# user-auth
# --------------------------------------------------------------------------- #
def test_userauth_merges_config_and_secret(tmp_path):
    config = make_config(tmp_path, apps={"shop": "oidc"})
    write_app(tmp_path, "shop", [])
    (tmp_path / ".build" / "stack.json").write_text("{}")
    (config.config_dir / "providers").mkdir()
    (config.config_dir / "providers" / "user_auth_oidc.yaml").write_text(
        "issuer: https://id.example.com\nclient_id: shop-client\n"
    )
    source = FakeSource({"shop.OIDC_CLIENT_SECRET": "very-secret"})

    env_map = userauth.run(config, {}, source=source, out=io.StringIO())

    assert env_map["shop"]["OIDC_ISSUER"] == "https://id.example.com"
    assert env_map["shop"]["OIDC_CLIENT_ID"] == "shop-client"
    assert env_map["shop"]["OIDC_CLIENT_SECRET"] == "very-secret"


def test_userauth_no_apps_is_noop(tmp_path):
    config = make_config(tmp_path)
    (tmp_path / ".build").mkdir()
    (tmp_path / ".build" / "stack.json").write_text("{}")
    env_map = userauth.run(config, {}, source=FakeSource({}), out=io.StringIO())
    assert env_map == {}


def test_userauth_missing_stack_json_errors(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(FileNotFoundError):
        userauth.run(config, {}, source=FakeSource({}), out=io.StringIO())
