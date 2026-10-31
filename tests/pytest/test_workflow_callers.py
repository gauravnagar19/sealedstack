"""D-056: every shown/generated infra caller workflow grants `packages: read`."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_init():
    spec = importlib.util.spec_from_file_location("sealedstack_init", REPO / "scripts" / "init.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _permissions_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(r"permissions:\s*\{", ln)]


def test_generated_infra_workflow_grants_packages_read():
    text = _load_init().infra_workflow("example-owner", "0.6.0")
    lines = _permissions_lines(text)
    assert lines and all("packages: read" in ln for ln in lines)


def test_docs_snippets_grant_packages_read():
    for doc in ("docs/user-experience.md", "docs/getting-started.md"):
        lines = _permissions_lines((REPO / doc).read_text(encoding="utf-8"))
        assert lines, doc
        assert all("packages: read" in ln for ln in lines), doc


def test_ux_status_table_records_the_d056_amendment():
    text = (REPO / "docs" / "user-experience.md").read_text(encoding="utf-8")
    assert "D-056" in text.split("\n---\n", 1)[0]
    assert "### D-056" in (REPO / "docs" / "maintainers" / "decisions.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- Task 8
def _app_registry_step():
    import yaml

    doc = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
    return next(s for s in doc["jobs"]["setup"]["steps"] if s.get("id") == "registry")


def _run_registry_step(registry_var: str, owner: str, tmp_path):
    import subprocess

    step = _app_registry_step()
    out = tmp_path / "out"
    out.touch()
    result = subprocess.run(
        ["bash", "-c", step["run"]], capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "SEALEDSTACK_REGISTRY": registry_var, "REPO_OWNER": owner, "GITHUB_OUTPUT": str(out)},
    )
    return result, dict(line.split("=", 1) for line in out.read_text().splitlines())


def test_first_app_build_defaults_the_registry_to_the_owners_ghcr(tmp_path):
    step = _app_registry_step()
    assert "vars.SEALEDSTACK_REGISTRY" in step["env"]["SEALEDSTACK_REGISTRY"]
    assert "github.repository_owner" in step["env"]["REPO_OWNER"]
    assert ":?" not in step["run"], "the step must not fail while the variable is unset"
    result, outputs = _run_registry_step("", "Example-Owner", tmp_path)
    assert result.returncode == 0, result.stderr
    assert outputs == {"prefix": "ghcr.io/example-owner", "host": "ghcr.io"}  # lower case, as GHCR requires


def test_a_published_registry_variable_wins(tmp_path):
    result, outputs = _run_registry_step("ghcr.io/other", "you", tmp_path)
    assert result.returncode == 0 and outputs["prefix"] == "ghcr.io/other"


def test_no_placeholder_image_owner_remains():
    for rel in ("Dockerfile", "scripts/init.py", "docs/limitations.md"):
        assert "anomalyco" not in (REPO / rel).read_text(encoding="utf-8"), rel
    dockerfile = (REPO / "Dockerfile").read_text()
    assert "ARG TOOLBOX_IMAGE\n" in dockerfile and "TOOLBOX_OWNER" not in dockerfile


def test_init_derives_the_image_owner_from_the_repo_not_a_placeholder(tmp_path, monkeypatch):
    init = _load_init()
    monkeypatch.delenv("SEALEDSTACK_IMAGE_OWNER", raising=False)
    monkeypatch.setattr(init.subprocess if hasattr(init, "subprocess") else __import__("subprocess"), "run",
                        lambda *a, **k: type("R", (), {"stdout": "git@github.com:you/my-infra.git\n"})(), raising=False)
    assert init.git_owner() == "you"
