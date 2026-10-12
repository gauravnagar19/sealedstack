"""Ported adapters: firewall, gate-oidc, app-add, compute, state-init, secrets CLI."""
from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from sealedstack import appadd, compute, config as config_mod, firewall, gateoidc, secrets as secrets_mod, stateinit


def _stack(tmp_path: Path, slots=None, providers=None, servers=None, admin_keys=None):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    (cfg / "stack.yaml").write_text(
        yaml.safe_dump({"name": "example-infra", "cloudflare": {"account_id": "acct"}, "slots": slots or {"secrets": "env"}, "providers": providers or {}, "admin": {"ssh_keys": admin_keys or []}})
    )
    (cfg / "apps.yaml").write_text("{}\n")
    (cfg / "servers.yaml").write_text(yaml.safe_dump({"servers": servers or {}}))
    return config_mod.load_config(config_dir=cfg, code_root=tmp_path, env={})


# --------------------------------------------------------------------------- #
# firewall
# --------------------------------------------------------------------------- #
def test_firewall_applies_a_basic_auth_request(tmp_path, monkeypatch):
    spec = tmp_path / "netcup.yaml"
    spec.write_text(
        yaml.safe_dump(
            {
                "base_url": "https://api.example.com",
                "auth": {"type": "basic"},
                "firewall": {"method": "post", "path": "/servers/{server_id}/firewall", "body": '{"rules": {{rules}}}'},
            }
        )
    )
    captured = {}

    class FakeResponse:
        def read(self):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["auth"] = request.get_header("Authorization")
        captured["data"] = request.data
        return FakeResponse()

    monkeypatch.setattr(firewall.urllib.request, "urlopen", fake_urlopen)
    rc = firewall.run(
        {"SPEC_PATH": str(spec), "SERVER_ID": "42", "RULES_JSON": "[1]", "API_USERNAME": "u", "API_PASSWORD": "p"}
    )
    assert rc == 0
    assert captured["url"] == "https://api.example.com/servers/42/firewall"
    assert captured["method"] == "POST"
    assert captured["auth"].startswith("Basic ")
    assert b"{{rules}}" not in captured["data"] and b"[1]" in captured["data"]


def test_firewall_missing_password(tmp_path):
    spec = tmp_path / "netcup.yaml"
    spec.write_text(yaml.safe_dump({"base_url": "https://x", "firewall": {"method": "post", "path": "/x"}, "auth": {"type": "basic"}}))
    assert firewall.run({"SPEC_PATH": str(spec), "SERVER_ID": "1"}) == 1


# --------------------------------------------------------------------------- #
# gate-oidc
# --------------------------------------------------------------------------- #
def test_gate_oidc_prints_wiring(tmp_path):
    cfg = tmp_path / "config" / "providers"
    cfg.mkdir(parents=True)
    (cfg / "gate_okta.yaml").write_text(
        yaml.safe_dump({"client_id": "cid", "gate_hostname": "gate.example.com", "org_url": "https://dev.example.com"})
    )
    out = io.StringIO()
    rc = gateoidc.run("okta", env={"SEALEDSTACK_CONFIG": str(tmp_path / "config")}, out=out)
    assert rc == 0
    text = out.getvalue()
    assert "https://dev.example.com/oauth2/default" in text
    assert "gate.example.com/oauth2/callback" in text
    assert "OKTA_CLIENT_SECRET" in text


def test_gate_oidc_missing_spec(tmp_path):
    rc = gateoidc.run("auth0", env={"SEALEDSTACK_CONFIG": str(tmp_path / "config")}, out=io.StringIO())
    assert rc == 1


# --------------------------------------------------------------------------- #
# app-add
# --------------------------------------------------------------------------- #
def test_appadd_writes_and_rejects_duplicates(tmp_path):
    config = _stack(tmp_path)
    appadd.add(config.config_dir, name="shop", repo="example/shop", hosts=["shop.example.com"], servers=["server-1"], access="owner-only")
    data = yaml.safe_load((config.config_dir / "apps.yaml").read_text())
    assert data["apps"]["shop"]["source"]["repo"] == "example/shop"
    assert data["apps"]["shop"]["access"] == "owner-only"
    with pytest.raises(config_mod.ConfigError):
        appadd.add(config.config_dir, name="shop", repo="example/shop", hosts=["h"], servers=["s"], access="owner-only")


# --------------------------------------------------------------------------- #
# compute
# --------------------------------------------------------------------------- #
def test_compute_tfvars_requires_selected_provider_credential(tmp_path):
    config = _stack(tmp_path, servers={"server-1": {"provider": "hetzner"}}, admin_keys=["ssh-ed25519 AAAA you@example.com"])
    source = secrets_mod.EnvSource({})
    with pytest.raises(config_mod.ConfigError):
        compute.tf_vars(config, {}, source, "server-1")


def test_compute_tfvars_sets_ssh_key_and_optional_tokens(tmp_path):
    config = _stack(tmp_path, servers={"server-1": {"provider": "netcup"}}, admin_keys=["ssh-ed25519 AAAA you@example.com"])
    source = secrets_mod.EnvSource({"HCLOUD_TOKEN": "hc", "NETCUP_API_USERNAME": "nu", "NETCUP_API_PASSWORD": "np"})
    variables = compute.tf_vars(config, {}, source, "server-1")
    assert variables["TF_VAR_ssh_public_key"] == "ssh-ed25519 AAAA you@example.com"
    assert variables["TF_VAR_hcloud_token"] == "hc"
    assert variables["TF_VAR_aws_region"] == "eu-central-1"  # default


# --------------------------------------------------------------------------- #
# state-init
# --------------------------------------------------------------------------- #
def test_stateinit_rejects_short_passphrase(tmp_path):
    config = _stack(tmp_path)
    source = secrets_mod.EnvSource(
        {"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "state_passphrase": "short"}
    )
    with pytest.raises(config_mod.ConfigError):
        stateinit.run(config, {}, source=source, runner=lambda argv: 0)


# --------------------------------------------------------------------------- #
# secrets CLI backend
# --------------------------------------------------------------------------- #
def test_cli_source_aws_ssm_via_runner(tmp_path):
    config = _stack(tmp_path, slots={"secrets": "aws_ssm"})
    calls = []

    def runner(argv):
        calls.append(argv)
        return 0, "s3cr3t", ""

    source = secrets_mod.build_source(config.stack, config.config_dir, {}, cli_runner=runner)
    result = source.get("R2_ACCESS_KEY_ID")
    assert result.found and result.value == "s3cr3t"
    assert calls and "ssm" in calls[0] and "/sealedstack/infra/R2_ACCESS_KEY_ID" in calls[0]


def test_cli_source_gcp_unsupported(tmp_path):
    config = _stack(tmp_path, slots={"secrets": "gcp"})
    source = secrets_mod.build_source(config.stack, config.config_dir, {}, cli_runner=lambda a: (0, "", ""))
    assert source.get("X").status == "error"
