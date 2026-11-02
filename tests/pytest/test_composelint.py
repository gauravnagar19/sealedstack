"""D-063: the compose host-escape lint (release job, check, plan and apply)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from sealedstack import composelint, preflight
from test_engine_preflight import APPS, STACK, by_id, make_config, make_ctx

REPO = Path(__file__).resolve().parents[2]


def problems(service, **extra):
    return composelint.lint({"services": {"web": {"image": "ghcr.io/example/web:1", **service}}, **extra})


# -- refused ------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("service", "needle"),
    [
        ({"privileged": True}, "privileged"),
        ({"privileged": "true"}, "privileged"),
        ({"privileged": "${PRIV}"}, "privileged"),
        ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock"]}, "runtime socket"),
        ({"volumes": ["/var/run/docker.sock:/var/run/docker.sock:ro"]}, "runtime socket"),
        ({"volumes": ["/run/docker.sock:/sock"]}, "runtime socket"),
        ({"volumes": [{"type": "bind", "source": "/var/run/docker.sock", "target": "/s"}]}, "runtime socket"),
        ({"volumes": ["/:/host"]}, "host path '/'"),
        ({"volumes": ["/etc:/etc:ro"]}, "host path '/etc'"),
        ({"volumes": ["/var/lib/docker:/x"]}, "host path '/var/lib/docker'"),
        ({"volumes": ["/etc/localtime/../shadow:/x"]}, "host path '/etc/localtime/../shadow'"),
        ({"volumes": [{"type": "bind", "source": "/root", "target": "/r"}]}, "host path '/root'"),
        ({"volumes": ["~/data:/d"]}, "home directory"),
        ({"volumes": ["../../etc:/d"]}, "outside the compose directory"),
        ({"volumes": ["${DATA_DIR}:/d"]}, "only known at deploy time"),
        ({"network_mode": "host"}, "network_mode: host"),
        ({"pid": "host"}, "pid: host"),
        ({"ipc": "host"}, "ipc: host"),
        ({"uts": "host"}, "uts: host"),
        ({"userns_mode": "host"}, "userns_mode: host"),
        ({"cap_add": ["SYS_ADMIN"]}, "cap_add 'SYS_ADMIN'"),
        ({"cap_add": ["ALL"]}, "cap_add 'ALL'"),
        ({"cap_add": ["NET_BIND_SERVICE", "NET_ADMIN"]}, "cap_add 'NET_ADMIN'"),
        ({"cap_add": ["CAP_SYS_PTRACE"]}, "SYS_PTRACE"),
        ({"devices": ["/dev/sda:/dev/sda"]}, "devices"),
        ({"device_cgroup_rules": ["c 1:3 mr"]}, "device_cgroup_rules"),
        ({"security_opt": ["seccomp=unconfined"]}, "seccomp=unconfined"),
        ({"security_opt": ["apparmor:unconfined"]}, "apparmor:unconfined"),
        ({"security_opt": ["label=disable"]}, "label=disable"),
        ({"security_opt": ["no-new-privileges:false"]}, "no-new-privileges:false"),
    ],
)
def test_host_escape_settings_are_refused_with_the_service_named_and_a_fix(service, needle):
    found = problems(service)
    assert found, service
    assert any(needle in line for line in found), found
    assert all(line.startswith("service 'web': ") for line in found)
    assert all(("Remove" in line or "Use " in line or "use " in line) for line in found)  # says what to do


def test_a_bind_mount_through_a_named_volume_is_refused_too():
    found = composelint.lint({"services": {"web": {"image": "x", "volumes": ["data:/d"]}},
                              "volumes": {"data": {"driver": "local", "driver_opts": {"type": "none", "o": "bind", "device": "/"}}}})
    assert found == ["volume 'data': driver_opts bind-mount the host path '/'. Use a plain named volume"]


def test_every_service_is_checked_and_named():
    found = composelint.lint({"services": {"a": {"image": "x"}, "b": {"image": "x", "privileged": True}, "c": {"image": "x", "pid": "host"}}})
    assert [line.split(":")[0] for line in found] == ["service 'b'", "service 'c'"]


# -- accepted -----------------------------------------------------------------------
@pytest.mark.parametrize(
    "service",
    [
        {},
        {"privileged": False},
        {"privileged": "false"},
        {"volumes": ["data:/var/lib/data", "cache:/cache:ro"]},
        {"volumes": ["/etc/localtime:/etc/localtime:ro"]},
        {"volumes": [{"type": "bind", "source": "/etc/localtime", "target": "/etc/localtime", "read_only": True}]},
        {"volumes": [{"type": "volume", "source": "data", "target": "/d"}, {"type": "tmpfs", "target": "/tmp"}]},
        {"volumes": ["/var/lib/anonymous"]},
        {"volumes": ["../files/uploads:/uploads", "./config:/etc/app:ro"]},
        {"network_mode": "bridge"},
        {"network_mode": "service:db"},
        {"pid": "service:x"},
        {"cap_add": ["NET_BIND_SERVICE", "CHOWN", "SETUID", "SETGID"]},
        {"cap_drop": ["ALL"]},
        {"security_opt": ["no-new-privileges:true", "seccomp=builtin"]},
        {"user": "1000:1000", "read_only": True},
    ],
)
def test_ordinary_settings_are_accepted(service):
    assert problems(service) == []


def test_nothing_to_lint_is_fine():
    assert composelint.lint({}) == [] and composelint.lint(None) == [] and composelint.lint({"services": None}) == []


# -- the release job runs the same code ---------------------------------------------
def test_the_release_workflow_embeds_the_current_lint_source():
    done = subprocess.run([sys.executable, str(REPO / "scripts" / "embed_composelint.py"), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_release_job_lints_before_anything_reaches_dokploy():
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["release"]["steps"]
    names = [s.get("name", "") for s in steps]
    lint_at = next(i for i, n in enumerate(names) if n.startswith("Lint the deploy compose"))
    assert lint_at < names.index("Start the production deployment record") < names.index("Update release-owned fields and deploy")
    assert lint_at > names.index("Render the deploy compose at the tag")
    assert "if" not in steps[lint_at]  # runs in both build modes


def run_embedded(tmp_path, compose: dict, cwd_files=None):
    """Extract the embedded script from app.yml and run it exactly as the runner would."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["release"]["steps"] if s.get("name", "").startswith("Lint the deploy compose"))
    script = step["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / ".build").mkdir(exist_ok=True)
    (tmp_path / ".build" / "deploy-compose.yml").write_text(yaml.safe_dump(compose), encoding="utf-8")
    return subprocess.run([sys.executable, "-"], input=script, cwd=tmp_path, capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": ""})


def test_the_embedded_script_passes_a_clean_compose_and_fails_a_privileged_one(tmp_path):
    clean = run_embedded(tmp_path, {"services": {"web": {"image": "x", "volumes": ["data:/d"]}}})
    assert clean.returncode == 0 and "no host-escape settings" in clean.stdout
    bad = run_embedded(tmp_path, {"services": {"web": {"image": "x", "privileged": True, "volumes": ["/var/run/docker.sock:/s"]}}})
    assert bad.returncode == 1
    assert "service 'web': privileged" in bad.stderr and "runtime socket" in bad.stderr and "2 problem(s)" in bad.stderr


# -- check / plan / apply ------------------------------------------------------------
def app_checkout(tmp_path, compose: dict) -> Path:
    app = tmp_path / "app-src"
    app.mkdir()
    (app / "sealedstack.yaml").write_text(yaml.safe_dump({"name": "shop", "compose": "docker-compose.yml", "build": {"mode": "server"}}))
    (app / "docker-compose.yml").write_text(yaml.safe_dump(compose))
    return app


def repo_apps():
    return {"apps": {"shop": {"source": {"repo": "example/shop", "ref": "main"}, "servers": ["web-1"],
                              "hostnames": ["shop.example.com"], "access": "owner-only"}}}


def render_modules(tmp_path, monkeypatch, compose):
    app = app_checkout(tmp_path, compose)
    monkeypatch.setenv("SEALEDSTACK_LOCAL_APPS", f"example/shop={app}")
    config = make_config(tmp_path, apps=repo_apps())
    return config, make_ctx(tmp_path, config)


def test_check_fails_for_an_app_whose_compose_asks_for_the_host(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x", "privileged": True}}})
    results = by_id(preflight.check_config(ctx))
    row = results["config.compose_security"]
    assert row.status == "fail" and "app 'shop': service 'web': privileged" in row.detail
    assert "named volumes" in row.fix


def test_check_passes_a_clean_compose(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x"}}})
    assert by_id(preflight.check_config(ctx))["config.compose_security"].status == "ok"


def test_render_which_plan_and_apply_run_first_refuses_the_app(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x", "network_mode": "host"}}})
    _, _, render_mod, _ = preflight._load_script_modules(REPO)
    render_mod.CONFIG, render_mod.BUILD = config.config_dir, tmp_path / "build"
    with pytest.raises(render_mod.RenderError, match=r"(?s)asks for host access.*app 'shop': service 'web': network_mode: host"):
        render_mod.render_all(allow_unresolved=False)
    assert not (tmp_path / "build" / "apps" / "shop.json").exists()  # nothing half-rendered for plan/apply to act on


def test_validate_reports_it_as_an_error(tmp_path, monkeypatch):
    import lint_compose

    assert lint_compose.lint_security({"services": {"web": {"image": "x", "devices": ["/dev/kvm"]}}})
    assert lint_compose.lint_security({"services": {"web": {"image": "x"}}}) == []


def test_check_fails_when_two_apps_share_a_service_name_on_one_server(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x"}}})
    monkeypatch.setattr(preflight, "_shared_network_collisions", lambda rendered, lint=None: ["apps a, b both have a service named 'api'"])
    row = by_id(preflight.check_config(ctx))["config.service_names"]
    assert row.status == "fail" and "'api'" in row.detail and "<app>-<service>" in row.fix


def test_check_passes_when_service_names_are_distinct(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x"}}})
    assert by_id(preflight.check_config(ctx))["config.service_names"].status == "ok"


def test_an_app_whose_repo_has_no_manifest_yet_is_pending_not_failing(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x"}}})
    _, _, render_mod, _ = preflight._load_script_modules(REPO)
    render_mod.CONFIG, render_mod.BUILD = config.config_dir, tmp_path / "build"

    def missing(repo, ref):
        raise render_mod.RenderError("GitHub API returned HTTP 404 for repos/o/shop/contents/sealedstack.yaml?ref=main")

    monkeypatch.setattr(render_mod, "fetch_app_manifest", missing)
    rendered = render_mod.render_all(allow_unresolved=False)  # strict: still no error
    assert rendered["shop"]["_pending"] and rendered["shop"]["_disabled"]
    assert not (tmp_path / "build" / "apps" / "shop.json").exists()


def test_other_fetch_failures_still_fail_strict_render(tmp_path, monkeypatch):
    config, ctx = render_modules(tmp_path, monkeypatch, {"services": {"web": {"image": "x"}}})
    _, _, render_mod, _ = preflight._load_script_modules(REPO)
    render_mod.CONFIG, render_mod.BUILD = config.config_dir, tmp_path / "build"

    def offline(repo, ref):
        raise render_mod.RenderError("could not reach the GitHub API: boom")

    monkeypatch.setattr(render_mod, "fetch_app_manifest", offline)
    with pytest.raises(render_mod.RenderError, match="could not fetch manifest"):
        render_mod.render_all(allow_unresolved=False)


def test_the_manifest_schema_takes_a_secrets_provider_and_keeps_the_list_form():
    import json
    import jsonschema

    schema = json.loads((REPO / "schemas" / "sealedstack.schema.json").read_text())
    base = {"name": "paisa", "web": {"service": "web", "port": 80}}
    ok = lambda secrets: not list(jsonschema.Draft202012Validator(schema).iter_errors({**base, "secrets": secrets}))  # noqa: E731
    assert ok(["A_KEY"]) and ok({"keys": ["A_KEY"]}) and ok({"provider": "paisa", "keys": ["A_KEY"]})
    assert not ok({"provider": "bad name", "keys": ["A_KEY"]}) and not ok({"provider": "p"})
