"""D-041 / G14: CI joins the tailnet as tag:ci before it reaches a server or Dokploy."""
from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import ciexport, config as cfg, dokploy, preflight  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

WORKFLOWS = REPO / ".github" / "workflows"


def load(name: str) -> dict:
    doc = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as True
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def steps(doc: dict, job: str) -> list[dict]:
    return doc["jobs"][job]["steps"]


def index_of(step_list: list[dict], predicate) -> int:
    for i, step in enumerate(step_list):
        if predicate(step):
            return i
    raise AssertionError("step not found")


def is_ts(step: dict) -> bool:
    return str(step.get("uses", "")).startswith("tailscale/github-action@")


def permissions(doc: dict, job: str) -> dict:
    return {**(doc.get("permissions") or {}), **(doc["jobs"][job].get("permissions") or {})}


# --------------------------------------------------------------------------- infra.yml
def test_infra_run_job_joins_the_tailnet_before_any_engine_command():
    doc = load("infra.yml")
    run = steps(doc, "run")
    ts_idx = [i for i, s in enumerate(run) if is_ts(s)]
    assert ts_idx, "infra.yml has no tailscale/github-action step"
    command_step = index_of(run, lambda s: str(s.get("name", "")).startswith("sealedstack ${{ needs.resolve.outputs.command"))
    assert max(ts_idx) < command_step
    # nothing before the join may run the engine against a server (the fallback
    # reader only reads the secret source)
    for i, step in enumerate(run[:max(ts_idx)]):
        text = str(step.get("run", ""))
        assert "ci exec" not in text and "ci resolve" not in text
    # the fallback reader also runs before its join
    reader = index_of(run, lambda s: s.get("id") == "ts_secret")
    assert reader < max(ts_idx)


def test_infra_join_is_tag_ci_with_oidc_first_and_a_logged_fallback():
    doc = load("infra.yml")
    run = steps(doc, "run")
    oidc = next(s for s in run if is_ts(s) and "audience" in s["with"])
    assert oidc["with"]["tags"] == "tag:ci"
    assert oidc["if"] == "steps.ts.outputs.mode == 'oidc'"
    fallback = next(s for s in run if is_ts(s) and "oauth-secret" in s["with"])
    assert fallback["with"]["tags"] == "tag:ci"
    assert fallback["if"] == "steps.ts.outputs.mode == 'secret'"
    trust = next(s for s in run if s.get("id") == "ts")
    assert "falling back to the OAuth client secret" in trust["run"]
    assert "SEALEDSTACK_TS_OAUTH_CLIENT_ID" in trust["env"]["VAR_CLIENT_ID"]
    assert "SEALEDSTACK_TS_AUDIENCE" in trust["env"]["VAR_AUDIENCE"]


def test_infra_writes_and_status_need_id_token_and_host_networking():
    doc = load("infra.yml")
    assert permissions(doc, "run").get("id-token") == "write"
    command_step = next(s for s in steps(doc, "run") if str(s.get("name", "")).startswith("sealedstack ${{ needs.resolve.outputs.command"))
    assert "--network host" in command_step["run"]


def test_only_commands_that_reach_servers_join():
    doc = load("infra.yml")
    trust = next(s for s in steps(doc, "run") if s.get("id") == "ts")
    assert trust["if"] == "needs.resolve.outputs.mesh == 'true'"
    from sealedstack import infraci

    assert not infraci.Resolution(command="setup").needs_mesh
    assert not infraci.Resolution(command="add-app").needs_mesh
    for command in ("plan", "apply", "up", "status", "check", "restore", "drill", "drift"):
        assert infraci.Resolution(command=command).needs_mesh


# --------------------------------------------------------------------------- app.yml and drift.yml
def test_app_release_joins_before_it_calls_dokploy():
    doc = load("app.yml")
    release = steps(doc, "release")
    ts = index_of(release, is_ts)
    deploy = index_of(release, lambda s: "DOKPLOY_URL" in str(s.get("env", {})) and "api(" in str(s.get("run", "")))
    assert ts < deploy
    # D-057: app release jobs are tag:release (Dokploy API only), never tag:ci
    assert release[ts]["with"]["tags"] == "tag:release"
    assert release[ts]["with"]["oauth-client-id"] == "${{ vars.SEALEDSTACK_TS_RELEASE_CLIENT_ID }}"
    assert release[ts]["with"]["audience"] == "${{ vars.SEALEDSTACK_TS_RELEASE_AUDIENCE }}"
    assert permissions(doc, "release").get("id-token") == "write"


def test_app_release_workflow_never_uses_tag_ci():
    text = (WORKFLOWS / "app.yml").read_text(encoding="utf-8")
    assert "tag:ci" not in text
    doc = load("app.yml")
    tags = {s["with"]["tags"] for job in doc["jobs"].values() for s in job["steps"] if is_ts(s)}
    assert tags == {"tag:release"}


def test_infra_and_drift_workflows_use_tag_ci_only():
    for name in ("infra.yml", "drift.yml"):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        assert "tag:release" not in text, name
        doc = load(name)
        tags = {s["with"]["tags"] for job in doc["jobs"].values() for s in job["steps"] if is_ts(s)}
        assert tags == {"tag:ci"}, name


def test_drift_joins_before_planning():
    doc = load("drift.yml")
    plan = steps(doc, "plan")
    ts = index_of(plan, is_ts)
    run = index_of(plan, lambda s: s.get("id") == "plan")
    assert ts < run
    assert plan[ts]["with"]["tags"] == "tag:ci"
    assert "audience" in plan[ts]["with"]  # OIDC first (D-041)


# --------------------------------------------------------------------------- FQDN addressing
def fqdn_config(tmp_path, tailnet="tail1234.ts.net"):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump({"name": "example-infra", "mesh": {"tailnet": tailnet}}))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"server-1": {"provider": "any-ssh", "role": "control"}}}))
    (cdir / "apps.yaml").write_text("apps: {}\n")
    return cfg.load_config(cdir, REPO, {})


def test_dokploy_endpoint_uses_the_server_fqdn(tmp_path):
    config = fqdn_config(tmp_path)
    assert dokploy.control_host(config) == "server-1.tail1234.ts.net"
    bare = tmp_path / "bare"
    bare.mkdir()
    assert dokploy.control_host(fqdn_config(bare, tailnet="")) == "server-1"


def test_ansible_inventory_addresses_servers_by_fqdn(tmp_path, monkeypatch):
    import importlib.util

    config = fqdn_config(tmp_path)
    monkeypatch.setenv("SEALEDSTACK_CONFIG", str(config.config_dir))
    spec = importlib.util.spec_from_file_location("sealed_inventory", REPO / "ansible" / "inventory.py")
    inv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inv)
    hostvars = inv.build_inventory()["_meta"]["hostvars"]["server-1"]
    assert hostvars["ansible_host"] == "server-1.tail1234.ts.net"


def test_check_probes_the_server_fqdn(tmp_path):
    config = fqdn_config(tmp_path)
    seen = []
    ctx = preflight.CheckContext(
        config=config, env={}, http=None, secrets=EnvSource({}), registry={}, version="0",
        build_dir=tmp_path / "b", tcp_probe=lambda host, port, timeout: seen.append((host, port)) or True,
    )
    preflight.check_servers(ctx)
    assert seen == [("server-1.tail1234.ts.net", 22)]


# --------------------------------------------------------------------------- ci-export (OAuth fallback)
def test_ci_export_masks_and_writes_only_to_the_output_file(tmp_path):
    config = fqdn_config(tmp_path)
    out_file = tmp_path / "gh_output"
    buf = io.StringIO()
    rc = ciexport.export(config, {}, "tailscale_oauth_client_secret", "client_secret", out_file,
                         source=EnvSource({"TAILSCALE_OAUTH_CLIENT_SECRET": "s3cr3t-value"}), out=buf)
    assert rc == 0
    assert buf.getvalue().strip() == "::add-mask::s3cr3t-value"
    assert out_file.read_text() == "client_secret=s3cr3t-value\n"


def test_ci_export_refuses_anything_outside_the_allow_list(tmp_path, capsys):
    config = fqdn_config(tmp_path)
    rc = ciexport.export(config, {}, "state_passphrase", "x", tmp_path / "o",
                         source=EnvSource({"STATE_PASSPHRASE": "0123456789abcdef"}))
    assert rc == 2 and not (tmp_path / "o").exists()
    assert "0123456789abcdef" not in capsys.readouterr().out


def test_ci_export_reports_a_missing_secret_with_both_names(tmp_path, capsys):
    config = fqdn_config(tmp_path)
    assert ciexport.export(config, {}, "tailscale_oauth_client_id", "client_id", tmp_path / "o", source=EnvSource({})) == 1
    assert "tailscale_oauth_client_id (source key TAILSCALE_OAUTH_CLIENT_ID)" in capsys.readouterr().err


def test_cli_routes_debug_ci_export_with_its_flags(tmp_path):
    from sealedstack import cli

    calls = []
    rc = cli.run(
        ["debug", "ci-export", "tailscale_oauth_client_id", "--name", "client_id", "--output", "/out"],
        code_root=REPO,
        env={"SEALEDSTACK_CONFIG": str(fqdn_config(tmp_path).config_dir)},
        runner=lambda argv, root: calls.append(list(argv)) or 0,
    )
    assert rc == 0
    assert "sealedstack.ciexport" in calls[0]
    assert "--name" in calls[0] and "--output" in calls[0]


def test_the_scheduled_drift_workflow_stays_off_unless_enabled():
    """D-059: drift.yml only runs when SEALEDSTACK_DRIFT_ENABLED is 'true'."""
    doc = load("drift.yml")
    assert doc["jobs"]["plan"]["if"] == "${{ vars.SEALEDSTACK_DRIFT_ENABLED == 'true' }}"
