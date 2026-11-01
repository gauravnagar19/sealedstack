"""Services that read the managed database join dokploy-network (live run 2026-11-01: `getaddrinfo ENOTFOUND demo-app-db-...`).

Dokploy attaches only the service that has the domain to dokploy-network; the Postgres service lives there by name.
Both renderers must agree: scripts/render.py seeds the compose, the release job in app.yml overwrites it at every release.
"""
from __future__ import annotations

import re
import subprocess
import sys
import textwrap

import yaml

from test_engine_server import REPO

sys.path.insert(0, str(REPO / "scripts"))
import lint_compose  # noqa: E402

MANIFEST = {"needs": {"postgres": {"version": "16", "database": "demoapp", "env": "DATABASE_URL"}}}
COMPOSE = {
    "services": {
        "ui": {"image": "x/ui:${APP_VERSION}", "depends_on": ["api"]},
        "api": {"image": "x/api:${APP_VERSION}", "environment": {"DATABASE_URL": "${DATABASE_URL}", "PORT": 3000}},
    }
}


def check(out):
    assert out["services"]["api"]["networks"] == ["default", "dokploy-network"]
    assert "networks" not in out["services"]["ui"]  # Dokploy joins the web service itself; the ui does not need the database
    assert out["networks"] == {"dokploy-network": {"external": True}}


def test_render_attaches_only_the_services_that_read_a_platform_variable():
    check(lint_compose.attach_platform_network(COMPOSE, MANIFEST))


def test_existing_networks_are_extended_not_replaced():
    compose = {"services": {"api": {"image": "x", "environment": ["DATABASE_URL=${DATABASE_URL}"], "networks": ["back"]}}, "networks": {"back": {}}}
    out = lint_compose.attach_platform_network(compose, MANIFEST)
    assert out["services"]["api"]["networks"] == ["back", "dokploy-network"]
    assert out["networks"]["back"] == {} and out["networks"]["dokploy-network"] == {"external": True}


def test_no_needs_means_no_change():
    assert lint_compose.attach_platform_network(COMPOSE, {}) == COMPOSE


def test_the_input_compose_is_not_mutated():
    before = yaml.safe_dump(COMPOSE)
    lint_compose.attach_platform_network(COMPOSE, MANIFEST)
    assert yaml.safe_dump(COMPOSE) == before


def test_the_release_job_renders_the_same_compose(tmp_path):
    """Run the inline python of app.yml's 'Render the deploy compose' step against a sample app."""
    text = (REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    step = text[text.index("- name: Render the deploy compose at the tag"):]
    code = step[step.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n"): step.index("\n          PY")]
    (tmp_path / "sealedstack.yaml").write_text(yaml.safe_dump({"compose": "docker-compose.yml", **MANIFEST}))
    (tmp_path / "docker-compose.yml").write_text(yaml.safe_dump(COMPOSE))
    subprocess.run([sys.executable, "-c", textwrap.dedent(code)], cwd=tmp_path, check=True)
    release = yaml.safe_load((tmp_path / ".build" / "deploy-compose.yml").read_text())
    check(release)
    assert release == lint_compose.attach_platform_network(lint_compose.strip_build(COMPOSE), MANIFEST)


def test_shared_network_names_are_the_web_service_and_the_database_consumers():
    manifest = {**MANIFEST, "web": {"service": "ui", "port": 80}}
    assert lint_compose.shared_network_services(COMPOSE, manifest) == {"ui", "api"}
    assert lint_compose.shared_network_services(COMPOSE, {}) == set()


def test_two_apps_publishing_the_same_name_on_one_server_are_warned_about():
    from sealedstack import preflight

    def app(servers):
        return {"_compose": COMPOSE, "manifest": {**MANIFEST, "web": {"service": "ui"}}, "web": {"service": "ui"}, "servers": servers}

    warnings = preflight._shared_network_collisions({"a": app(["s1"]), "b": app(["s1"]), "c": app(["s2"])})
    assert len(warnings) == 2 and all("apps a, b" in w and "s1" in w for w in warnings)
    assert preflight._shared_network_collisions({"a": app(["s1"]), "c": app(["s2"])}) == []
