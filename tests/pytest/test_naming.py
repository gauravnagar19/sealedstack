"""One name per app (D-089)."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sealedstack import appadd, naming, preflight

REPO = Path(__file__).resolve().parents[2]


def lint_compose():
    spec = importlib.util.spec_from_file_location("lint_compose", REPO / "scripts" / "lint_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("repo,service,want", [
    ("paisa", "paisa-web", "paisa-web"),
    ("paisa", "paisa", "paisa"),
    ("paisa", "web", "paisa-web"),
    ("demo-app", "web", "demo-app-web"),
    ("paisa", "paisaweb", "paisa-paisaweb"),
])
def test_image_names_do_not_repeat_the_repo(repo, service, want):
    assert naming.image_repo(repo, service) == want
    assert lint_compose().image_repo(repo, service) == want  # the embedded lint and the engine agree


def test_lint_accepts_the_new_and_the_old_image_form_and_nothing_else():
    lint = lint_compose()
    compose = lambda image: {"services": {"paisa-web": {"build": ".", "image": image}}}
    kw = dict(registry="ghcr.io", owner="me", repo="paisa")
    assert lint.lint_build(compose("ghcr.io/me/paisa-web:${APP_VERSION}"), "ci", **kw) == []
    assert lint.lint_build(compose("ghcr.io/me/paisa-paisa-web:${APP_VERSION}"), "ci", **kw) == []
    assert lint.lint_build(compose("ghcr.io/me/other:${APP_VERSION}"), "ci", **kw)


@pytest.mark.parametrize("repo,name", [("me/paisa", "paisa"), ("me/My_App", "my-app"), ("me/demo.app", "demo-app")])
def test_the_name_comes_from_the_repo(repo, name):
    assert naming.app_name_from_repo(repo) == name


def test_access_defaults_only_to_a_single_policy():
    assert naming.access_default({"gate": {"policies": {"owner-only": {}}}}) == "owner-only"
    assert naming.access_default({"gate": {"policies": {"a": {}, "b": {}}}}) is None
    assert naming.access_default({}) is None


def test_deviations_name_each_difference_and_what_it_touches():
    rendered = {
        "manifest": {"name": "paisa-app"},
        "source": {"repo": "me/paisa-repo"},
        "vault": {"provider": "paisa-secrets"},
    }
    compose = "services:\n  paisa-repo-web:\n    image: ghcr.io/me/paisa-repo-paisa-repo-web:${APP_VERSION}\n"
    found = naming.deviations("paisa", rendered, compose)
    levels = [level for level, _ in found]
    text = " | ".join(t for _, t in found)
    assert levels[0] == "warn" and "set name: paisa" in text
    assert "recreates the Dokploy project" in text
    assert "drop secrets.provider" in text
    assert "'paisa-repo-web'" in text


def test_a_conforming_app_has_no_deviations():
    rendered = {"manifest": {"name": "paisa"}, "source": {"repo": "me/paisa"}, "vault": {"provider": "paisa"}}
    compose = "services:\n  web:\n    image: ghcr.io/me/paisa-web:${APP_VERSION}\n"
    assert naming.deviations("paisa", rendered, compose) == []


def test_check_reports_per_app(tmp_path):
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "paisa.json").write_text(json.dumps({"manifest": {"name": "other"}, "source": {"repo": "me/paisa"}}))
    (apps / "demo.json").write_text(json.dumps({"manifest": {"name": "demo"}, "source": {"repo": "me/demo"}}))
    (apps / "hello.json").write_text(json.dumps({"_synthesized": True}))
    results = {r.id: r for r in preflight.check_names(SimpleNamespace(build_dir=tmp_path))}
    assert results["config.names.paisa"].status == "warn"
    assert results["config.names.demo"].status == "ok"
    assert "config.names.hello" not in results


def test_the_add_app_pr_shows_the_derived_names():
    body = appadd.render_infra_body(
        name="paisa", app_repo="me/paisa", hosts=["paisa.example.com"], servers=["server-1"], access="owner-only",
        manifest={"secrets": {"keys": ["A"]}}, app_pr=1, domain="example.com",
    )
    assert "| Dokploy project and compose | `paisa` |" in body
    assert "| Secrets provider and Doppler project | `paisa` |" in body
    assert "| Default hostname | `paisa.example.com` |" in body


def test_the_rendered_name_is_always_the_apps_yaml_key(monkeypatch):
    spec = importlib.util.spec_from_file_location("render", REPO / "scripts" / "render.py")
    render = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(render)
    manifest = {"name": "something-else", "secrets": {"keys": ["A"]}}
    monkeypatch.setattr(render, "resolve_manifest", lambda name, entry, allow_unresolved: (manifest, {"synthesized": False, "unresolved": False}))
    rendered = render.build_app("paisa", {"source": {"repo": "me/paisa", "ref": "main"}}, allow_unresolved=True, registry={})
    assert rendered["name"] == "paisa"
    assert rendered["vault"]["provider"] == "paisa"
