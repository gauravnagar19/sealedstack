"""D-054: R2 S3 credentials derived from the Cloudflare API token."""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import cli  # noqa: E402
from sealedstack import config as cfg  # noqa: E402
from sealedstack import edge, platform, preflight, r2, stateinit, tofu  # noqa: E402
from sealedstack.http import Route, StubClient  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

TOKEN = "cf-token-value-0001"
TOKEN_ID = "example-token-id-0001"
EXPECTED_SECRET = hashlib.sha256(TOKEN.encode()).hexdigest()


@pytest.fixture(autouse=True)
def _clear_cache():
    r2._TOKEN_ID_CACHE.clear()
    yield
    r2._TOKEN_ID_CACHE.clear()


def make_config(tmp_path, *, keys=None, account="acct123"):
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    stack = {
        "name": "example-infra",
        "domain": "example.com",
        "slots": {"secrets": "env", "compute": "any-ssh", "mesh": "tailscale", "ingress": "cloudflare",
                  "gate": "cloudflare_access", "backup": "r2", "platform": "dokploy"},
        "cloudflare": {"account_id": account, "zone": "example.com", "team_name": "example"},
        "state": {"backend": "r2", "bucket": "sealedstack-state"},
        "secrets": {"project": "infra", "keys": keys or {}},
    }
    (cdir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (cdir / "servers.yaml").write_text("servers: {}\n")
    (cdir / "apps.yaml").write_text("apps: {}\n")
    (tmp_path / "tofu" / "stacks" / "edge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tofu" / "stacks" / "platform").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tofu" / "stacks" / "state").mkdir(parents=True, exist_ok=True)
    return cfg.load_config(config_dir=cdir, code_root=tmp_path, env={})


def verify_http(token_id=TOKEN_ID):
    return StubClient().add("tokens/verify", Route(json_body={"success": True, "result": {"id": token_id, "status": "active"}}))


# --------------------------------------------------------------------------- derivation
def test_derivation_is_correct_for_a_known_token_and_id(tmp_path):
    config = make_config(tmp_path)
    creds = r2.resolve(EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN}), config, {}, http=verify_http())
    assert creds.derived is True
    assert creds.access_key_id == TOKEN_ID
    assert creds.secret_access_key == EXPECTED_SECRET
    # the documented definition: secret = SHA-256 hex of the token value
    assert len(creds.secret_access_key) == 64


def test_verify_uses_the_bearer_token(tmp_path):
    config = make_config(tmp_path)
    http = verify_http()
    r2.resolve(EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN}), config, {}, http=http)
    assert http.requests[0]["url"].endswith("/user/tokens/verify")
    assert http.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_account_owned_token_falls_back_to_the_account_verify_endpoint(tmp_path):
    config = make_config(tmp_path)
    http = (
        StubClient()
        .add("/user/tokens/verify", Route(status=401, json_body={"success": False}))
        .add("/accounts/acct123/tokens/verify", Route(json_body={"success": True, "result": {"id": "acctok"}}))
    )
    creds = r2.resolve(EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN}), config, {}, http=http)
    assert creds.access_key_id == "acctok"


def test_token_id_is_verified_once_per_process(tmp_path):
    config = make_config(tmp_path)
    http = verify_http()
    source = EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN})
    r2.resolve(source, config, {}, http=http)
    r2.resolve(source, config, {}, http=http)
    assert len(http.requests) == 1


# --------------------------------------------------------------------------- override
def test_an_explicit_override_wins_and_makes_no_request(tmp_path):
    config = make_config(tmp_path)
    http = verify_http()
    source = EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN, "R2_ACCESS_KEY_ID": "ovr-id", "R2_SECRET_ACCESS_KEY": "ovr-secret"})
    creds = r2.resolve(source, config, {}, http=http)
    assert (creds.access_key_id, creds.secret_access_key, creds.derived) == ("ovr-id", "ovr-secret", False)
    assert http.requests == []


def test_override_honours_renamed_keys(tmp_path):
    config = make_config(tmp_path, keys={"R2_ACCESS_KEY_ID": "MY_R2_ID", "R2_SECRET_ACCESS_KEY": "MY_R2_SECRET"})
    creds = r2.resolve(EnvSource({"MY_R2_ID": "a", "MY_R2_SECRET": "b"}), config, {}, http=verify_http())
    assert (creds.access_key_id, creds.secret_access_key) == ("a", "b")


def test_a_half_override_is_an_error_naming_the_missing_key(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(r2.R2Error) as err:
        r2.resolve(EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN, "R2_ACCESS_KEY_ID": "a"}), config, {}, http=verify_http())
    assert "R2_SECRET_ACCESS_KEY" in str(err.value)


# --------------------------------------------------------------------------- failure modes
def test_missing_token_is_an_error_when_required_and_none_when_optional(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(r2.R2Error) as err:
        r2.resolve(EnvSource({}), config, {}, http=verify_http())
    assert "CLOUDFLARE_API_TOKEN" in str(err.value) and "Workers R2 Storage: Edit" in str(err.value)
    assert r2.resolve(EnvSource({}), config, {}, http=verify_http(), required=False) is None


def test_unverifiable_token_fails_with_the_fix(tmp_path):
    config = make_config(tmp_path)
    http = StubClient().add("tokens/verify", Route(status=401, json_body={"success": False}))
    with pytest.raises(r2.R2Error) as err:
        r2.resolve(EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN}), config, {}, http=http)
    assert "Workers R2 Storage: Edit" in str(err.value) and "HTTP 401" in str(err.value)


# --------------------------------------------------------------------------- check is honest
CF_ROUTES = [
    ("zones?name=example.com", Route(json_body={"success": True, "result": [{"id": "zone123"}]})),
    ("/dns_records", Route(json_body={"success": True})),
    ("/cfd_tunnel", Route(json_body={"success": True})),
    ("/access/apps", Route(json_body={"success": True})),
    ("rulesets?per_page=1", Route(json_body={"success": True})),
    ("/bot_management", Route(json_body={"success": True})),
    ("http_request_dynamic_redirect", Route(json_body={"success": True})),
    ("access/organizations", Route(json_body={"success": True})),
]


def cf_http(r2_status=200):
    http = StubClient().add("tokens/verify", Route(json_body={"success": True, "result": {"id": TOKEN_ID}}))
    http.add("/r2/buckets", Route(status=r2_status, json_body={"success": r2_status == 200}))
    for needle, route in CF_ROUTES:
        http.add(needle, route)
    return http


def check_ctx(tmp_path, env, http, config=None):
    config = config or make_config(tmp_path)
    return preflight.CheckContext(
        config=config, env=dict(env), http=http, secrets=EnvSource(env), registry={},
        version="0.0.0", build_dir=tmp_path / "b", tcp_probe=lambda *a: False,
    )


def test_check_says_derived_only_when_derivation_succeeds(tmp_path):
    results = {r.id: r for r in preflight.check_cloudflare(check_ctx(tmp_path, {"CLOUDFLARE_API_TOKEN": TOKEN}, cf_http()))}
    assert results["cloudflare.r2_derived"].status == "ok"
    assert "derived" in results["cloudflare.r2_derived"].detail


def test_check_fails_with_the_fix_when_the_token_lacks_r2_permission(tmp_path):
    results = {r.id: r for r in preflight.check_cloudflare(check_ctx(tmp_path, {"CLOUDFLARE_API_TOKEN": TOKEN}, cf_http(403)))}
    derived = results["cloudflare.r2_derived"]
    assert derived.status == "fail"
    assert "Workers R2 Storage: Edit" in derived.fix and "CLOUDFLARE_API_TOKEN" in derived.fix


def test_check_accepts_an_override_without_the_permission(tmp_path):
    env = {"CLOUDFLARE_API_TOKEN": TOKEN, "R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b"}
    results = {r.id: r for r in preflight.check_cloudflare(check_ctx(tmp_path, env, cf_http(403)))}
    assert results["cloudflare.r2_derived"].status == "ok"
    assert "override" in results["cloudflare.r2_derived"].detail


def test_check_no_longer_requires_the_r2_keys(tmp_path):
    registry = {"backup": {"implementations": {"r2": {"status": "experimental", "optional_secrets": ["R2_ACCESS_KEY_ID"]}}}}
    assert preflight.gather_needed_secrets({"slots": {"backup": "r2"}}, {}, registry) == []


def test_real_registry_has_no_required_r2_keys():
    _, registry_mod, _, _ = preflight._load_script_modules(REPO)
    needed = preflight.gather_needed_secrets({"slots": {"backup": "r2"}}, {}, registry_mod.load())
    assert [name for name, _ in needed] == []


def test_check_reads_the_cloudflare_token_from_the_secret_source_not_only_env(tmp_path):
    # CI exports only BWS_ACCESS_TOKEN; the token lives in the source (renamed key).
    config = make_config(tmp_path, keys={"CLOUDFLARE_API_TOKEN": "MY_CF_TOKEN"})
    ctx = check_ctx(tmp_path, {"MY_CF_TOKEN": TOKEN}, cf_http(), config=config)
    results = {r.id: r for r in preflight.check_cloudflare(ctx)}
    assert results["cloudflare.token"].status == "ok"
    assert results["cloudflare.r2_derived"].status == "ok"


# --------------------------------------------------------------------------- used everywhere
def test_tofu_backend_config_uses_derived_keys(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN, "state_passphrase": "0123456789abcdef"})
    path, passphrase = tofu.backend_config(config, {}, source, tmp_path / "tofu" / "stacks" / "edge", http=verify_http())
    try:
        body = path.read_text()
        assert os.stat(path).st_mode & 0o777 == 0o600
    finally:
        path.unlink()
    assert f'access_key = "{TOKEN_ID}"' in body
    assert f'secret_key = "{EXPECTED_SECRET}"' in body
    assert passphrase == "0123456789abcdef"


def test_tofu_backend_config_override_wins(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"R2_ACCESS_KEY_ID": "oid", "R2_SECRET_ACCESS_KEY": "osec", "state_passphrase": "0123456789abcdef"})
    path, _ = tofu.backend_config(config, {}, source, tmp_path / "tofu" / "stacks" / "edge", http=StubClient())
    try:
        body = path.read_text()
    finally:
        path.unlink()
    assert 'access_key = "oid"' in body and 'secret_key = "osec"' in body


def test_stateinit_passes_derived_keys_to_tofu(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"CLOUDFLARE_API_TOKEN": TOKEN, "state_passphrase": "0123456789abcdef"})
    env = stateinit.child_env(config, {}, source, http=verify_http())
    assert env["TF_VAR_r2_access_key_id"] == TOKEN_ID
    assert env["TF_VAR_r2_secret_access_key"] == EXPECTED_SECRET
    calls = []
    assert stateinit.run(config, {}, source=EnvSource({"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b",
                                                      "state_passphrase": "0123456789abcdef"}),
                         runner=lambda argv: calls.append(argv) or 0) == 0
    assert len(calls) == 2


def test_edge_and_platform_tfvars_use_derived_keys(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({
        "CLOUDFLARE_API_TOKEN": TOKEN, "TAILSCALE_OAUTH_CLIENT_ID": "i", "TAILSCALE_OAUTH_CLIENT_SECRET": "s",
        "DOKPLOY_API_KEY": "k",
    })
    e = edge.tf_vars(config, {"BWS_ACCESS_TOKEN": "t"}, source, http=verify_http())
    assert e["TF_VAR_backup_access_key_id"] == TOKEN_ID
    assert e["TF_VAR_backup_secret_access_key"] == EXPECTED_SECRET
    p = platform.tf_vars(config, {}, source, http=verify_http())
    assert p["TF_VAR_object_storage_access_key_id"] == TOKEN_ID
    assert p["TF_VAR_backup_secret_access_key"] == EXPECTED_SECRET


def test_state_lock_in_cli_uses_derived_keys(tmp_path):
    config = make_config(tmp_path)
    lock = cli._state_lock(config, {"CLOUDFLARE_API_TOKEN": TOKEN}, verify_http())
    assert lock is not None


def test_state_lock_falls_back_with_a_reason_and_no_key_material(tmp_path, capsys):
    config = make_config(tmp_path)
    http = StubClient().add("tokens/verify", Route(status=401, json_body={"success": False}))
    assert cli._state_lock(config, {"CLOUDFLARE_API_TOKEN": TOKEN}, http) is None
    err = capsys.readouterr().err
    assert "remote state lock unavailable" in err
    assert TOKEN not in err and EXPECTED_SECRET not in err


# --------------------------------------------------------------------------- nothing derived is printed
def test_nothing_derived_appears_in_any_output(tmp_path, capsys):
    results = preflight.check_cloudflare(check_ctx(tmp_path, {"CLOUDFLARE_API_TOKEN": TOKEN}, cf_http()))
    blob = preflight.render_markdown(results) + preflight.render_json(results)
    for needle in (EXPECTED_SECRET, EXPECTED_SECRET[:8], TOKEN):
        assert needle not in blob
    creds = r2.R2Credentials("id", "super-secret-value", derived=True)
    assert "super-secret-value" not in repr(creds) and "super-secret-value" not in str(creds)
    assert json.dumps({"x": 1})  # sanity
    out = capsys.readouterr()
    assert EXPECTED_SECRET not in out.out + out.err


def test_no_source_file_prints_secret_derived_material():
    # The old code printed the first 8 hex chars of the derived secret.
    text = (REPO / "engine" / "sealedstack" / "preflight.py").read_text()
    assert "[:8]" not in text
