"""Preflight rows the Setup checklist depends on (Task 6)."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import config as cfg, preflight  # noqa: E402
from sealedstack.secrets import BitwardenSource, EnvSource  # noqa: E402


def make_ctx(tmp_path, *, secrets, env=None, files=None, tailnet="tail1234.ts.net"):
    root = tmp_path / "infra"
    (root / "config").mkdir(parents=True)
    stack = {"name": "x", "domain": "example.com", "owner_email": "you@example.com",
             "slots": {"compute": "any-ssh", "mesh": "tailscale", "ingress": "cloudflare", "gate": "cloudflare_access",
                       "secrets": "bitwarden", "backup": "r2", "platform": "dokploy"},
             "mesh": {"tailnet": tailnet, "policy": "unmanaged"}}
    (root / "config" / "stack.yaml").write_text(yaml.safe_dump(stack))
    (root / "config" / "servers.yaml").write_text("servers: {}\n")
    (root / "config" / "apps.yaml").write_text("apps: {}\n")
    for rel, text in (files or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    config = cfg.load_config(root / "config", REPO, {})
    return preflight.build_context(config, env=env or {}, secrets=secrets, version="0.6.0", build_dir=root / ".build")


def by_id(results):
    return {r.id: r for r in results}


def test_a_missing_bitwarden_token_is_one_clear_row(tmp_path):
    ctx = make_ctx(tmp_path, secrets=BitwardenSource({}, runner=lambda a: (1, "", "")))
    row = by_id(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "BWS_ACCESS_TOKEN" in row.detail and "read-only" in row.fix


def test_a_reachable_source_is_ok(tmp_path):
    ctx = make_ctx(tmp_path, secrets=EnvSource({}), env={})
    assert by_id(preflight.check_secrets(ctx))["secrets.source"].status == "ok"


def test_version_pins_must_match(tmp_path):
    workflow = ".github/workflows/sealedstack.yml"
    ok_files = {"sealedstack.version": "0.6.0\n", workflow: "uses: you/sealedstack/.github/workflows/infra.yml@v0.6.0\n"}
    bad_files = {"sealedstack.version": "0.6.0\n", workflow: "uses: you/sealedstack/.github/workflows/infra.yml@v0.5.0\n"}
    ok = by_id(preflight.check_versions(make_ctx(tmp_path / "a", secrets=EnvSource({}), files=ok_files)))
    bad = by_id(preflight.check_versions(make_ctx(tmp_path / "b", secrets=EnvSource({}), files=bad_files)))
    assert ok["versions.pin"].status == "ok"
    assert bad["versions.pin"].status == "fail" and "bump" in bad["versions.pin"].fix


def test_no_pin_row_outside_an_infra_repo(tmp_path):
    assert "versions.pin" not in by_id(preflight.check_versions(make_ctx(tmp_path, secrets=EnvSource({}))))


def test_the_bot_row(tmp_path):
    """D-058: the bot identity is a fine-grained token in GH_TOKEN; the App is not used yet."""
    none = by_id(preflight.check_bot(make_ctx(tmp_path / "a", secrets=EnvSource({}))))["forge.bot"]
    assert none.status == "fail" and "fine-grained personal access token" in none.fix and "Pull requests: read and write" in none.fix
    token = by_id(preflight.check_bot(make_ctx(tmp_path / "b", secrets=EnvSource({}), env={"GH_TOKEN": "github_pat_example"})))
    assert token["forge.bot"].status == "ok" and "fine-grained token" in token["forge.bot"].detail
    # the removed SEALEDSTACK_BOT_TOKEN_SET flag no longer stands in for a token
    flag = by_id(preflight.check_bot(make_ctx(tmp_path / "b2", secrets=EnvSource({}), env={"SEALEDSTACK_BOT_TOKEN_SET": "true"})))
    assert flag["forge.bot"].status == "fail"
    assert "BOT_TOKEN_SET" not in (REPO / "engine" / "sealedstack" / "preflight.py").read_text(encoding="utf-8")
    # the workflow's built-in token (what GH_TOKEN falls back to) is not a bot identity
    builtin = by_id(preflight.check_bot(make_ctx(tmp_path / "d", secrets=EnvSource({}), env={"GH_TOKEN": "ghs_builtin"})))["forge.bot"]
    assert builtin.status == "fail" and "built-in token" in builtin.detail
    # App credentials are not used yet, so they do not satisfy the row
    app = {"SEALEDSTACK_BOT_APP_ID": "1", "SEALEDSTACK_BOT_PRIVATE_KEY": "k", "SEALEDSTACK_BOT_INSTALLATION_ID": "2"}
    only_app = by_id(preflight.check_bot(make_ctx(tmp_path / "c", secrets=EnvSource(app))))["forge.bot"]
    assert only_app.status == "fail" and "not used yet (D-058)" in only_app.detail


def test_the_tailnet_and_policy_are_not_reported_as_config_errors(tmp_path):
    ctx = make_ctx(tmp_path, secrets=EnvSource({}), tailnet="")
    ctx.config.stack["mesh"] = {"policy": "managed", "tailnet": ""}
    ctx.config.servers["servers"] = {"server-1": {"provider": "any-ssh", "role": "control", "os": "ubuntu-24.04"}}
    results = by_id(preflight.check_config(ctx))
    assert results["config.cross_file"].status == "ok", results["config.cross_file"].detail
    ts = by_id(preflight.check_tailscale(ctx))
    assert ts["tailscale.tailnet"].status == "fail" and ts["tailscale.policy"].status == "fail"
