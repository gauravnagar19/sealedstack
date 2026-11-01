"""D-082: ci.yml is one job graph; what it may and may not do is enforced here."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"


def ci() -> dict:
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


def test_the_pipeline_is_one_workflow_and_the_old_ones_are_gone():
    assert ci()["name"] == "ci"  # release.yml looks up the run by this file name (D-081)
    assert not (WORKFLOWS / "reference-app.yml").exists()
    assert set(ci()["jobs"]) == {"prepare", "lint", "tests", "tofu", "docs", "reference-app"}


def test_every_check_waits_for_prepare_and_toolbox_jobs_use_its_image():
    jobs = ci()["jobs"]
    for name, job in jobs.items():
        if name != "prepare":
            assert job["needs"] == "prepare", name
    for name in ("lint", "tests", "tofu"):
        step = next(s for s in jobs[name]["steps"] if s.get("uses") == "./.github/actions/toolbox")
        assert step["with"]["image"] == "${{ needs.prepare.outputs.image }}"
    # nothing rebuilds the toolbox outside the one fallback in prepare and the action
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert text.count("context: toolbox") == 1


def test_ci_never_pushes_and_never_has_package_write():
    data = ci()
    assert data["permissions"] == {"contents": "read", "packages": "read"}
    for name, job in data["jobs"].items():
        assert job.get("permissions", {}).get("packages") != "write", name
        assert job.get("uses") != "./.github/workflows/toolbox.yml", f"{name}: toolbox.yml pushes to GHCR when the tag is missing"
        for step in job.get("steps", []):
            assert str((step.get("with") or {}).get("push", "false")).lower() != "true", f"{name}: {step.get('name')}"
    for path in [WORKFLOWS / "ci.yml", REPO / ".github" / "actions" / "toolbox" / "action.yml"]:
        text = path.read_text(encoding="utf-8")
        assert "push: true" not in text and "docker push" not in text, path.name


def test_pull_requests_replace_older_runs_but_main_pushes_are_never_cancelled():
    concurrency = ci()["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    group = concurrency["group"]
    # a push's group is unique to its run, so it can neither cancel nor be cancelled
    assert "github.run_id" in group and "pull_request.number" in group


def test_docs_are_built_once_by_docs_yml_with_the_pages_permissions_it_needs():
    job = ci()["jobs"]["docs"]
    assert job["uses"] == "./.github/workflows/docs.yml"
    assert job["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    docs = yaml.safe_load((WORKFLOWS / "docs.yml").read_text(encoding="utf-8"))
    triggers = docs.get("on", docs.get(True))
    assert "push" not in triggers  # ci.yml is what runs it after a push, so it is not built twice
    assert "workflow_call" in triggers


def test_the_tofu_job_validates_in_the_toolbox_with_a_provider_cache_keyed_on_the_lock_files():
    steps = ci()["jobs"]["tofu"]["steps"]
    cache = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache@"))
    assert "hashFiles('tofu/stacks/*/.terraform.lock.hcl')" in cache["with"]["key"]
    assert not any("setup-opentofu" in str(s.get("uses", "")) for s in steps)  # the toolbox pins tofu
    assert any(s.get("run") == "in-toolbox scripts/tofu-validate.sh" for s in steps)


# -- the change detection in `prepare` -------------------------------------------------------------
def run_changes(tmp_path: Path, before_files: dict, after_files: dict, event="push", usable_base=True) -> dict:
    script = next(s for s in ci()["jobs"]["prepare"]["steps"] if s.get("id") == "changes")["run"]
    repo = tmp_path / "r"
    repo.mkdir(parents=True)

    def git(*args):
        return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args], capture_output=True, text=True, check=True).stdout.strip()

    git("init", "-q", "-b", "main")
    for rel, text in before_files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    base = git("rev-parse", "HEAD")
    for rel, text in after_files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git("add", "-A")
    git("commit", "-q", "-m", "two")
    out = tmp_path / "out"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(tmp_path), "GITHUB_OUTPUT": str(out), "EVENT": event,
           "PR_BASE": base if usable_base else "", "PUSH_BEFORE": base if usable_base else "0" * 40}
    result = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines())


BASE = {"docs/a.md": "a", "tofu/stacks/x.tf": "x", "scripts/s.py": "s", "examples/app-template/Dockerfile": "d"}


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_a_docs_only_change_skips_tofu_and_the_docker_jobs(tmp_path, event):
    out = run_changes(tmp_path, BASE, {"docs/a.md": "b", "README.md": "r"}, event=event)
    assert out == {"docs": "true", "tofu": "false", "images": "false"}


def test_a_tofu_change_runs_tofu_but_not_docs_or_the_docker_jobs(tmp_path):
    assert run_changes(tmp_path, BASE, {"tofu/stacks/x.tf": "y"}) == {"docs": "false", "tofu": "true", "images": "false"}


def test_an_example_app_change_runs_the_docker_jobs_only(tmp_path):
    assert run_changes(tmp_path, BASE, {"examples/app-template/Dockerfile": "e"}) == {"docs": "false", "tofu": "false", "images": "true"}


def test_a_change_to_ci_itself_runs_everything(tmp_path):
    assert run_changes(tmp_path, BASE, {".github/workflows/ci.yml": "x"}) == {"docs": "true", "tofu": "true", "images": "true"}
    assert run_changes(tmp_path / "b", BASE, {".github/actions/toolbox/in-toolbox": "x"}) == {"docs": "false", "tofu": "true", "images": "true"}


def test_without_a_usable_base_everything_runs(tmp_path):
    assert run_changes(tmp_path, BASE, {"docs/a.md": "b"}, usable_base=False) == {"docs": "true", "tofu": "true", "images": "true"}
    assert run_changes(tmp_path / "w", BASE, {"docs/a.md": "b"}, event="workflow_dispatch") == {"docs": "true", "tofu": "true", "images": "true"}
