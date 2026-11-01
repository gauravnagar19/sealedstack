"""edge + tofu backend port (Task 4.9 / D-047)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, edge, secrets as secrets_mod
from sealedstack.secrets import SecretResult


class FakeSource(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data: dict):
        self.data = data

    def get(self, name: str) -> SecretResult:
        if name in self.data:
            return SecretResult(name, "found", self.data[name])
        return SecretResult(name, "missing", detail="not in fake source")


def make_config(tmp_path: Path) -> config_mod.Config:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "stack.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "example-infra",
                "cloudflare": {"account_id": "acct123", "zone_id": "zone123"},
                "state": {"bucket": "example-state"},
                "slots": {"secrets": "bitwarden"},
            }
        )
    )
    (cfg_dir / "apps.yaml").write_text("{}\n")
    (cfg_dir / "servers.yaml").write_text("servers: {}\n")
    (tmp_path / "tofu" / "stacks" / "edge").mkdir(parents=True)
    return config_mod.load_config(config_dir=cfg_dir, code_root=tmp_path, env={})


REQUIRED = {
    "CLOUDFLARE_API_TOKEN": "cf",
    "TAILSCALE_OAUTH_CLIENT_ID": "id",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
    # explicit R2 override: no token-verify request in unit tests (D-054)
    "R2_ACCESS_KEY_ID": "rid",
    "R2_SECRET_ACCESS_KEY": "rsecret",
}


def test_tfvars_requires_cloudflare_token(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(config_mod.ConfigError):
        edge.tf_vars(config, {"BWS_ACCESS_TOKEN": "t"}, FakeSource({}))


def test_tfvars_backup_defaults_from_account(tmp_path):
    config = make_config(tmp_path)
    source = FakeSource({**REQUIRED, "R2_ACCESS_KEY_ID": "rac", "R2_SECRET_ACCESS_KEY": "rsec"})
    variables = edge.tf_vars(config, {"BWS_ACCESS_TOKEN": "t"}, source)
    assert variables["TF_VAR_backup_endpoint"] == "https://acct123.r2.cloudflarestorage.com"
    assert variables["TF_VAR_backup_access_key_id"] == "rac"
    assert variables["TF_VAR_backup_region"] == "auto"


def test_edge_apply_writes_outputs(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    monkeypatch.setattr(edge.tofu_mod, "ready", lambda *a, **k: None)
    ran = []

    def runner(argv):
        ran.append(argv)
        return 0

    rc = edge.run(
        config,
        {"BWS_ACCESS_TOKEN": "t"},
        "apply",
        source=FakeSource(REQUIRED),
        runner=runner,
        output_runner=lambda argv: (0, json.dumps({"tunnel": {"edge": "connector-token"}})),
    )

    assert rc == 0
    assert ran[0][3] == "apply"  # tofu -chdir <dir> apply [flags]
    written = json.loads((tmp_path / ".build" / "edge-outputs.json").read_text())
    assert written["tunnel"]["edge"] == "connector-token"


def test_tofu_output_wrappers_are_unwrapped_for_the_platform_stack():
    """Live run 2026-11-01: platform read edge.backup and got {value, type, sensitive} instead of {bucket, endpoint, region}."""
    raw = json.dumps({
        "backup": {"value": {"bucket": "b", "endpoint": "https://e", "region": "auto"}, "type": ["object", {}], "sensitive": False},
        "ingress_type": {"value": "cloudflare", "type": "string", "sensitive": False},
        "connector_tokens": {"value": {"hello": "tok"}, "type": ["map", "string"], "sensitive": True},
    })
    plain = edge.plain_outputs(raw)
    assert plain["backup"]["bucket"] == "b" and plain["ingress_type"] == "cloudflare" and plain["connector_tokens"] == {"hello": "tok"}
    assert edge.plain_outputs("") == {} and edge.plain_outputs("{}") == {}
    assert edge.plain_outputs(json.dumps({"x": {"edge": "t"}})) == {"x": {"edge": "t"}}  # an already plain value stays


# -- D-078: security response headers on the app hostnames ---------------------------------------------------------------
import re  # noqa: E402

EDGE_TF = (Path(__file__).resolve().parents[2] / "tofu" / "stacks" / "edge" / "main.tf").read_text(encoding="utf-8")


def _block(name: str) -> str:
    text = EDGE_TF[EDGE_TF.index(f'resource "cloudflare_ruleset" "{name}"'):]
    return text[: text.index("\n}\n") + 3]


def test_the_edge_stack_sets_permissions_policy_on_the_app_hostnames_only():
    block = _block("security_headers")
    assert 'phase   = "http_response_headers_transform"' in block
    assert '"Permissions-Policy"' in block and "camera=()" in block
    assert "local.security_headers_expression" in block and "http.host in" in EDGE_TF
    assert "local.security_headers_on" in block  # the opt-out


def test_no_default_content_security_policy_or_frame_options():
    """A policy that fits one app breaks another's inline scripts or embedding; apps send their own."""
    code = "\n".join(line for line in _block("security_headers").splitlines() if not line.lstrip().startswith("#"))
    assert "Content-Security-Policy" not in code and "X-Frame-Options" not in code


def test_the_switch_is_optional_in_the_schema_and_on_by_default():
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "stack.schema.json").read_text(encoding="utf-8"))
    edge_security = schema["properties"]["edge_security"]
    assert edge_security["properties"]["security_headers"]["default"] is True
    assert "security_headers" not in edge_security["required"]  # existing stack.yaml files stay valid
    assert re.search(r"security_headers_on\s*=\s*try\(local\.stack\.edge_security\.security_headers, true\)", EDGE_TF)


def test_check_probes_the_transform_rules_permission():
    from sealedstack import preflight

    ids = {entry[0]: entry for entry in preflight.CF_PERMISSIONS}
    assert ids["headers_edit"][1] == "Zone · Transform Rules: Edit"
    assert "http_response_headers_transform" in ids["headers_edit"][2]


# -- D-079: TLS floor and ciphers ---------------------------------------------------------------------------------------
def test_the_zone_floor_is_tls_12_and_cbc_sha_suites_are_not_offered():
    assert re.search(r'min_tls_version\s*=\s*try\(local\.stack\.edge_security\.min_tls_version, "1\.2"\)', EDGE_TF)
    ciphers = EDGE_TF[EDGE_TF.index("tls_ciphers = ["):]
    ciphers = ciphers[: ciphers.index("]")]
    assert "GCM" in ciphers and "CHACHA20" in ciphers
    assert "CBC" not in ciphers and not re.search(r"-SHA\"", ciphers)  # plain -SHA suites are CBC


def test_tls_settings_are_zone_settings_gated_on_cloudflare_and_the_opt_out():
    assert 'setting_id = "min_tls_version"' in EDGE_TF and 'setting_id = "ciphers"' in EDGE_TF
    ciphers_block = EDGE_TF[EDGE_TF.index('resource "cloudflare_zone_setting" "ciphers"'):]
    assert "local.restrict_ciphers" in ciphers_block[: ciphers_block.index("\n}\n")]


def test_schema_accepts_only_real_tls_versions_and_hsts_is_in_the_header_rule():
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "stack.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]["edge_security"]["properties"]
    assert props["min_tls_version"]["enum"] == ["1.0", "1.1", "1.2", "1.3"] and props["min_tls_version"]["default"] == "1.2"
    assert "min_tls_version" not in schema["properties"]["edge_security"]["required"]
    block = _block("security_headers")
    assert '"Strict-Transport-Security"' in block and "max-age=31536000" in block
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    assert "includeSubDomains" not in code and "preload" not in code


def test_custom_ciphers_are_off_by_default_because_they_need_advanced_certificate_manager():
    """0.6.17 applied `ciphers` by default: Cloudflare answered 400 / 1023 on a zone without the paid add-on and the whole apply stopped."""
    assert re.search(r"restrict_ciphers\s*=\s*try\(local\.stack\.edge_security\.restrict_tls_ciphers, false\)", EDGE_TF)
    schema = json.loads((Path(__file__).resolve().parents[2] / "schemas" / "stack.schema.json").read_text(encoding="utf-8"))
    assert schema["properties"]["edge_security"]["properties"]["restrict_tls_ciphers"]["default"] is False
    example = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "stack.example.yaml").read_text(encoding="utf-8"))
    assert example["edge_security"]["restrict_tls_ciphers"] is False
    assert "min_tls_version" in EDGE_TF and 'setting_id = "min_tls_version"' in EDGE_TF  # the TLS floor stays on regardless


def test_check_probes_zone_settings_permission():
    from sealedstack import preflight

    assert any(e[0] == "zone_settings_edit" and "Zone Settings: Edit" in e[1] for e in preflight.CF_PERMISSIONS)
