"""The reference app (examples/app-template) is real, runnable and valid (M3)."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "examples" / "app-template"
INFRA = REPO / "examples" / "reference-infra" / "config"


def load_server():
    spec = importlib.util.spec_from_file_location("reference_server", APP / "app" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def server():
    module = load_server()
    httpd = module.make_server(0, "127.0.0.1")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield module, httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def get(port: int, path: str):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
def test_health_is_200_without_any_configuration(server, monkeypatch):
    for var in ("DATABASE_URL", "REDIS_URL", "EXAMPLE_SECRET", "APP_VERSION"):
        monkeypatch.delenv(var, raising=False)
    _module, port = server
    status, body = get(port, "/api/health")
    assert status == 200
    assert json.loads(body) == {
        "status": "ok",
        "version": "dev",
        "database": "not configured",
        "redis": "not configured",
        "secret": "not configured",
    }


def test_health_reports_configuration_but_never_its_values(server, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:hunter2@db:5432/example")
    monkeypatch.setenv("REDIS_URL", "redis://:topsecret@cache:6379/0")
    monkeypatch.setenv("EXAMPLE_SECRET", "s3cr3t-value")
    monkeypatch.setenv("APP_VERSION", "v0.0.7")
    _module, port = server
    status, body = get(port, "/api/health")
    data = json.loads(body)
    assert status == 200 and data["version"] == "v0.0.7"
    assert data["database"] == data["redis"] == data["secret"] == "configured"
    for secret in ("hunter2", "topsecret", "s3cr3t-value", "postgres://"):
        assert secret not in body


def test_root_serves_a_page_and_unknown_paths_404(server):
    _module, port = server
    assert get(port, "/")[0] == 200
    assert get(port, "/api/health?probe=1")[0] == 200
    assert get(port, "/missing")[0] == 404


# --------------------------------------------------------------------------- #
# the files a developer and the platform rely on
# --------------------------------------------------------------------------- #
def test_compose_follows_the_platform_rules_and_keeps_build_for_dev():
    compose = yaml.safe_load((APP / "docker-compose.yml").read_text(encoding="utf-8"))
    web = compose["services"]["web"]
    assert "ports" not in web  # D-001: traffic arrives through the tunnel
    assert "build" in web and web["image"] == "ghcr.io/example-owner/example-web:${APP_VERSION}"  # D-024, D-030
    assert "postgres" not in json.dumps(compose["services"])  # the database is declared under needs:


class ComposeLoader(yaml.SafeLoader):
    """Safe loader that accepts Compose's merge tags (``!reset``, ``!override``)."""


ComposeLoader.add_multi_constructor("!", lambda loader, suffix, node: None)


def test_override_is_for_local_development_only():
    override = yaml.load((APP / "docker-compose.override.yml").read_text(encoding="utf-8"), Loader=ComposeLoader)
    assert override["services"]["web"]["ports"] == ["3000:3000"]
    assert "${APP_VERSION:-dev}" in override["services"]["web"]["image"]
    manifest = yaml.safe_load((APP / "sealedstack.yaml").read_text(encoding="utf-8"))
    assert manifest["compose"] == "docker-compose.yml"  # sealedstack never reads the override


def test_manifest_matches_the_app_and_the_dockerfile():
    manifest = yaml.safe_load((APP / "sealedstack.yaml").read_text(encoding="utf-8"))
    assert manifest["web"] == {"service": "web", "port": 3000, "health": "/api/health"}
    dockerfile = (APP / "Dockerfile").read_text(encoding="utf-8")
    assert "EXPOSE 3000" in dockerfile and "ENV PORT=3000" in dockerfile
    assert "\nUSER app" in dockerfile  # not root
    assert "server.py" in dockerfile and (APP / "app" / "server.py").is_file()


# --------------------------------------------------------------------------- #
# validation against the sample infra config, offline (what the CI job runs)
# --------------------------------------------------------------------------- #
def run_validate(app_dir: Path, build_dir: Path, config_dir: Path = INFRA):
    env = dict(os.environ)
    # never write into the repo's own .build/ (or anywhere else in the checkout)
    env.update(
        SEALEDSTACK_BUILD_DIR=str(build_dir),
        SEALEDSTACK_CONFIG=str(config_dir),
        SEALEDSTACK_LOCAL_APPS=f"example-owner/example={app_dir}",
        TF_VAR_state_passphrase="ci-dummy-passphrase",
    )
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "validate.py")], capture_output=True, text=True, env=env, cwd=str(REPO)
    )


def test_reference_app_validates_against_the_sample_infra_config(tmp_path):
    result = run_validate(APP, tmp_path / "build")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "validate: ok" in result.stdout


def copy_app(tmp_path) -> Path:
    target = tmp_path / "app"
    shutil.copytree(APP, target, ignore=shutil.ignore_patterns(".github"))
    return target


def test_validation_fails_when_ports_are_published(tmp_path):
    app = copy_app(tmp_path)
    compose = app / "docker-compose.yml"
    compose.write_text(compose.read_text().replace("    restart: unless-stopped", "    restart: unless-stopped\n    ports:\n      - \"3000:3000\""))
    result = run_validate(app, tmp_path / "build")
    assert result.returncode != 0 and "ports" in (result.stdout + result.stderr)


def test_validation_fails_when_a_built_service_is_not_pinned(tmp_path):
    app = copy_app(tmp_path)
    compose = app / "docker-compose.yml"
    compose.write_text("\n".join(line for line in compose.read_text().splitlines() if not line.strip().startswith("image:")) + "\n")
    result = run_validate(app, tmp_path / "build")
    assert result.returncode != 0 and "image" in (result.stdout + result.stderr)


def test_validation_fails_for_an_in_compose_database(tmp_path):
    app = copy_app(tmp_path)
    compose = app / "docker-compose.yml"
    compose.write_text(compose.read_text().replace("\nnetworks:", "  db:\n    image: postgres:16\n\nnetworks:", 1))
    result = run_validate(app, tmp_path / "build")
    assert result.returncode != 0 and "postgres" in (result.stdout + result.stderr)


def test_local_apps_mapping_is_read_by_render(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    import render

    os.environ["SEALEDSTACK_LOCAL_APPS"] = f"a/b={tmp_path},c/d={APP}"
    try:
        assert render.local_app_dir("c/d") == APP
        assert render.local_app_dir("x/y") is None
        (tmp_path / "f.txt").write_text("hi")
        assert render.fetch_repo_file("a/b", "main", "f.txt") == "hi"
        with pytest.raises(render.RenderError, match="not found in the local checkout"):
            render.fetch_repo_file("a/b", "main", "missing.txt")
    finally:
        del os.environ["SEALEDSTACK_LOCAL_APPS"]
