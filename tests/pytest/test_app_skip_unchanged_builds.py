"""D-093: app.yml can reuse the previous version's image for a service whose inputs did not change.

The planner is the inline python of the `Plan which images to build or reuse` step; it is extracted and run exactly
as the runner would, against a real git repository, with `docker` replaced by a shim so no registry is needed."""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))

SERVICES = ["paisa-api", "paisa-web", "paisa-devsnap"]
INPUTS = {
    "paisa-api": ["apps/api/**", "packages/**", "package.json", "pnpm-lock.yaml"],
    "paisa-web": ["apps/ui/**", "packages/shared/**", "package.json", "pnpm-lock.yaml"],
    "paisa-devsnap": ["devsnap/**"],
}
IGNORE = ["docs/**", "**/*.md", "tests/**"]
VERSION_ONLY = ["apps/api/package.json", "apps/ui/app.json"]


def plan_script() -> str:
    step = next(s for s in WORKFLOW["jobs"]["setup"]["steps"] if s.get("id") == "plan")
    return step["run"].split("<<'PY' >> \"$GITHUB_OUTPUT\"\n", 1)[1].rsplit("\nPY", 1)[0]


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True, capture_output=True)


def write(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, "apps/api/src/a.ts", "1")
    write(repo, "apps/api/package.json", json.dumps({"name": "api", "version": "0.0.1", "dependencies": {"x": "1"}}))
    write(repo, "apps/ui/src/u.ts", "1")
    write(repo, "apps/ui/app.json", json.dumps({"expo": {"name": "ui", "version": "0.0.1"}}))
    write(repo, "packages/shared/s.ts", "1")
    write(repo, "packages/db/d.ts", "1")
    write(repo, "devsnap/run.sh", "1")
    write(repo, "docs/x.md", "1")
    write(repo, "tests/api/t.ts", "1")
    write(repo, "pnpm-lock.yaml", "1")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "tag", "v0.0.5")
    return repo


def run_plan(repo: Path, tmp_path: Path, mode="on", docker_ok=True, previous="v0.0.5", inputs=INPUTS, event="push"):
    shim = tmp_path / "bin"
    shim.mkdir(exist_ok=True)
    (shim / "docker").write_text("#!/bin/sh\nexit %d\n" % (0 if docker_ok else 1))
    (shim / "docker").chmod(0o755)
    output = tmp_path / "out"
    summary = tmp_path / "summary"
    output.write_text("")
    summary.write_text("")
    env = {
        **os.environ, "PATH": f"{shim}{os.pathsep}{os.environ['PATH']}", "PYTHONPATH": "",
        "EVENT": event, "MODE": mode, "PREVIOUS": previous, "REGISTRY": "ghcr.io/o", "PACKAGE": "paisa",
        "BUILD_INPUTS": json.dumps(inputs), "BUILD_IGNORE": json.dumps(IGNORE), "BUILD_VERSION_ONLY": json.dumps(VERSION_ONLY),
        "MATRIX": json.dumps({"include": [{"service": n, "context": ".", "dockerfile": "", "target": ""} for n in SERVICES]}),
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    done = subprocess.run([sys.executable, "-"], input=plan_script(), cwd=repo, capture_output=True, text=True, env=env)
    assert done.returncode == 0, done.stderr
    out = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)
    built = [e["service"] for e in json.loads(out["build_matrix"])["include"]]
    return built, out["reuse_services"].split(), done.stderr


def commit(repo: Path, message="change"):
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", message)


# -- the decision -----------------------------------------------------------------------------
def test_a_ui_only_change_reuses_api_and_devsnap(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/ui/src/u.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert built == ["paisa-web"] and sorted(reused) == ["paisa-api", "paisa-devsnap"]


def test_a_db_package_change_rebuilds_api_but_not_web_or_devsnap(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "packages/db/d.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert built == ["paisa-api"] and sorted(reused) == ["paisa-devsnap", "paisa-web"]


def test_shared_package_change_rebuilds_api_and_web(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "packages/shared/s.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert sorted(built) == ["paisa-api", "paisa-web"] and reused == ["paisa-devsnap"]


def test_docs_and_tests_change_nothing_so_every_image_is_reused(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "docs/x.md", "2")
    write(repo, "tests/api/t.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert built == [] and sorted(reused) == sorted(SERVICES)


def test_a_version_bump_alone_is_not_a_change(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/api/package.json", json.dumps({"name": "api", "version": "0.0.2", "dependencies": {"x": "1"}}))
    write(repo, "apps/ui/app.json", json.dumps({"expo": {"name": "ui", "version": "0.0.2"}}))
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert built == [] and sorted(reused) == sorted(SERVICES)


def test_a_version_bump_together_with_a_dependency_change_is_a_change(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/api/package.json", json.dumps({"name": "api", "version": "0.0.2", "dependencies": {"x": "2"}}))
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert built == ["paisa-api"] and sorted(reused) == ["paisa-devsnap", "paisa-web"]


def test_a_file_no_service_claims_rebuilds_everything(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "turbo.json", "{}")
    commit(repo)
    built, reused, stderr = run_plan(repo, tmp_path)
    assert sorted(built) == sorted(SERVICES) and reused == []
    assert "turbo.json is not listed for any image" in stderr


def test_the_lockfile_rebuilds_the_services_that_list_it(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "pnpm-lock.yaml", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path)
    assert sorted(built) == ["paisa-api", "paisa-web"] and reused == ["paisa-devsnap"]


def test_a_service_without_declared_inputs_is_always_built(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/ui/src/u.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path, inputs={k: v for k, v in INPUTS.items() if k != "paisa-devsnap"})
    assert sorted(built) == ["paisa-devsnap", "paisa-web"] and reused == ["paisa-api"]


def test_the_build_all_marker_in_the_head_commit_forces_a_full_build(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "docs/x.md", "2")
    commit(repo, "docs only [build-all]")
    built, reused, _ = run_plan(repo, tmp_path)
    assert sorted(built) == sorted(SERVICES) and reused == []


def test_a_previous_image_missing_from_the_registry_is_built_instead(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/ui/src/u.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path, docker_ok=False)
    assert sorted(built) == sorted(SERVICES) and reused == []


def test_no_previous_version_builds_everything(tmp_path):
    repo = make_repo(tmp_path)
    built, reused, _ = run_plan(repo, tmp_path, previous="")
    assert sorted(built) == sorted(SERVICES) and reused == []


def test_an_unreachable_previous_tag_builds_everything(tmp_path):
    repo = make_repo(tmp_path)
    built, reused, _ = run_plan(repo, tmp_path, previous="v9.9.9")
    assert sorted(built) == sorted(SERVICES) and reused == []


# -- the modes ---------------------------------------------------------------------------------
def test_off_builds_everything(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/ui/src/u.ts", "2")
    commit(repo)
    built, reused, _ = run_plan(repo, tmp_path, mode="off")
    assert sorted(built) == sorted(SERVICES) and reused == []


def test_dry_run_builds_everything_but_reports_what_it_would_reuse(tmp_path):
    repo = make_repo(tmp_path)
    write(repo, "apps/ui/src/u.ts", "2")
    commit(repo)
    built, reused, stderr = run_plan(repo, tmp_path, mode="dry-run")
    assert sorted(built) == sorted(SERVICES) and reused == []
    assert "| paisa-api | would reuse (dry-run, building anyway) |" in stderr and "| paisa-web | build |" in stderr
    assert (tmp_path / "summary").read_text().startswith("### Image build plan (dry-run)")


def test_an_unknown_mode_fails(tmp_path):
    repo = make_repo(tmp_path)
    shim = tmp_path / "bin"
    shim.mkdir()
    env = {**os.environ, "EVENT": "push", "MODE": "yes", "MATRIX": json.dumps({"include": []}), "PREVIOUS": "v0.0.5", "PYTHONPATH": ""}
    done = subprocess.run([sys.executable, "-"], input=plan_script(), cwd=repo, capture_output=True, text=True, env=env)
    assert done.returncode == 1 and "must be off, dry-run or on" in done.stderr


# -- the wiring --------------------------------------------------------------------------------
def test_skipping_is_opt_in_so_existing_callers_behave_as_before():
    inputs = WORKFLOW[True]["workflow_call"]["inputs"] if True in WORKFLOW else WORKFLOW["on"]["workflow_call"]["inputs"]
    assert inputs["skip_unchanged_builds"]["default"] == "off"
    assert inputs["build_inputs"]["default"] == "{}"


def test_build_uses_the_planned_matrix_and_tag_waits_for_the_retag_job():
    jobs = WORKFLOW["jobs"]
    assert "build_matrix" in jobs["build"]["strategy"]["matrix"] and "build_count" in jobs["build"]["if"]
    assert "reuse" in jobs["tag"]["needs"] and "reuse" in jobs["release"]["needs"]
    steps = jobs["reuse"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == "Retag the previous images")
    assert "docker buildx imagetools create" in run and "${{" not in run
    assert 'pyyaml' not in str(steps)  # the retag job needs no YAML parsing
