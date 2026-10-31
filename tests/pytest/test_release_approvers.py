"""DR-11: optional `release.approvers` in sealedstack.yaml; the release job refuses anyone else."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
SCHEMA = json.loads((REPO / "schemas" / "sealedstack.schema.json").read_text())


def step(job, name):
    return next(s for s in WORKFLOW["jobs"][job]["steps"] if s.get("name") == name)


def run_bash(script, env, cwd):
    return subprocess.run(["bash", "-c", script], cwd=cwd, env={**os.environ, **env}, capture_output=True, text=True)


# -- schema -------------------------------------------------------------------------------------
def errors(release):
    manifest = {"name": "shop", "web": {"service": "web", "port": 80}, "release": release}
    return list(jsonschema.Draft202012Validator(SCHEMA).iter_errors(manifest))


def test_the_manifest_accepts_a_list_of_github_users_and_nothing_else():
    assert errors({"approvers": ["you", "co-owner", "release-bot[bot]"]}) == []
    assert errors({}) == []
    assert errors({"approvers": ["bad name"]}) and errors({"approvers": ["-x"]}) and errors({"approvers": "you"}) and errors({"approvers": ["a", "a"]})
    assert errors({"approvers": ["you"], "other": 1})


# -- the check step ---------------------------------------------------------------------------------
GATE = step("release", "Only the listed approvers may release")


def gate(actor, approvers):
    return run_bash(GATE["run"], {"ACTOR": actor, "APPROVERS": approvers}, REPO)


def test_without_a_list_anyone_may_release_and_the_log_says_so():
    done = gate("anyone", "")
    assert done.returncode == 0 and "any user who can run this workflow may release" in done.stdout


def test_a_listed_approver_may_release_case_insensitively():
    assert gate("You", "you co-owner").returncode == 0
    assert gate("co-owner", "you co-owner").returncode == 0


def test_anyone_else_is_refused_with_a_clear_error():
    done = gate("mallory", "you co-owner")
    assert done.returncode == 1 and "::error::mallory is not in release.approvers" in done.stdout


def test_a_prefix_or_substring_of_an_approver_does_not_pass():
    assert gate("yo", "you").returncode == 1 and gate("you2", "you").returncode == 1


def test_the_gate_is_the_first_step_of_the_release_job_and_takes_the_actor_through_env():
    first = WORKFLOW["jobs"]["release"]["steps"][0]
    assert first is GATE or first == GATE
    assert GATE["env"]["ACTOR"] == "${{ github.actor }}" and GATE["env"]["APPROVERS"] == "${{ needs.setup.outputs.release_approvers }}"
    assert "${{" not in GATE["run"]


# -- where the list comes from ---------------------------------------------------------------------------
APPROVERS_STEP = step("setup", "Release approvers (read from the default branch)")


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path):
    """A clone whose origin/main has one manifest and whose current branch has another."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "sealedstack.yaml").write_text(yaml.safe_dump({"name": "shop", "web": {"service": "web", "port": 80}, "release": {"approvers": ["you", "co-owner"]}}))
    git(seed, "add", ".")
    git(seed, "commit", "-q", "-m", "init")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "checkout", "-q", "-b", "attacker")
    (clone / "sealedstack.yaml").write_text(yaml.safe_dump({"name": "shop", "web": {"service": "web", "port": 80}, "release": {"approvers": ["mallory"]}}))
    git(clone, "commit", "-q", "-am", "make myself an approver")
    return clone


def approvers_from(checkout, tmp_path, default_branch="main"):
    out = tmp_path / "ghout"
    out.write_text("")
    done = run_bash(APPROVERS_STEP["run"], {"DEFAULT_BRANCH": default_branch, "RUNNER_TEMP": str(tmp_path), "GITHUB_OUTPUT": str(out),
                                           "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"}, checkout)
    assert done.returncode == 0, done.stderr
    return out.read_text().strip()


def test_the_list_is_read_from_the_default_branch_not_from_the_branch_the_run_started_on(checkout, tmp_path):
    assert approvers_from(checkout, tmp_path) == "list=you co-owner"  # not "mallory"


def test_no_manifest_or_no_list_means_no_gate(checkout, tmp_path):
    assert approvers_from(checkout, tmp_path, default_branch="nope") == "list="
    git(checkout, "checkout", "-q", "main")
    (checkout / "sealedstack.yaml").write_text(yaml.safe_dump({"name": "shop", "web": {"service": "web", "port": 80}}))
    git(checkout, "commit", "-q", "-am", "drop")
    git(checkout, "push", "-q", "origin", "main")
    git(checkout, "fetch", "-q", "origin")
    assert approvers_from(checkout, tmp_path) == "list="


def test_names_that_are_not_plain_user_names_are_dropped(checkout, tmp_path):
    git(checkout, "checkout", "-q", "main")
    (checkout / "sealedstack.yaml").write_text(yaml.safe_dump({"name": "shop", "web": {"service": "web", "port": 80}, "release": {"approvers": ["ok-user", "bad name; rm -rf /", "$(id)"]}}))
    git(checkout, "commit", "-q", "-am", "x")
    git(checkout, "push", "-q", "origin", "main")
    git(checkout, "fetch", "-q", "origin")
    assert approvers_from(checkout, tmp_path) == "list=ok-user"


def test_the_setup_job_exports_the_list_and_the_release_job_needs_it():
    assert WORKFLOW["jobs"]["setup"]["outputs"]["release_approvers"] == "${{ steps.approvers.outputs.list }}"
    assert "setup" in WORKFLOW["jobs"]["release"]["needs"]


def test_the_limits_are_documented():
    text = (REPO / "docs" / "limitations.md").read_text(encoding="utf-8")
    assert "release.approvers" in text and "default branch" in text and "branch protection" in text
