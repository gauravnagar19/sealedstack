"""D-080: scripts/release.sh picks the next version, bumps, checks, pushes, tags and dispatches (two sessions kept picking the same number)."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "release.sh"


IDENTITY = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(cwd, *args, check=True):
    env = {**os.environ, **IDENTITY}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=check, text=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    """A bare origin at 0.6.14 (tagged), a clone with the script, and a fake `gh` that records its calls."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    work = tmp_path / "work"
    git(tmp_path, "clone", "-q", str(origin), str(work))
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / "release.sh")
    (work / "VERSION").write_text("0.6.14\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "release: 0.6.14")
    git(work, "branch", "-M", "main")
    git(work, "tag", "v0.6.14")
    git(work, "push", "-q", "origin", "main", "--tags")
    git(work, "checkout", "-q", "-b", "feature")
    (work / "change.txt").write_text("x\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "feat: a change")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    gh = bin_dir / "gh"
    gh.write_text(f'#!/bin/sh\necho "$@" >> {log}\n[ "$1 $2" = "run list" ] && echo 4242\nexit 0\n')
    gh.chmod(0o755)
    return {"origin": origin, "work": work, "log": log, "path": f"{bin_dir}:{os.environ['PATH']}", "tmp": tmp_path}


def release(w, *args, tests="true", checks="true"):
    env = {**os.environ, **IDENTITY, "PATH": w["path"], "RELEASE_TEST_CMD": tests, "RELEASE_CHECK_CMD": checks}
    return subprocess.run(["bash", "scripts/release.sh", *args], cwd=w["work"], env=env, text=True, capture_output=True)


def origin_tags(w):
    return git(w["origin"], "tag", "-l").stdout.split()


def test_a_dry_run_names_the_next_version_and_the_commits_and_changes_nothing(world):
    out = release(world, "--dry-run")
    assert out.returncode == 0, out.stderr
    assert "0.6.14 -> 0.6.15" in out.stdout and "feat: a change" in out.stdout
    assert (world["work"] / "VERSION").read_text() == "0.6.14\n"
    assert git(world["work"], "status", "--porcelain").stdout == ""
    assert not world["log"].exists()


def test_the_version_is_one_more_than_the_highest_tag_even_when_VERSION_is_behind(world):
    """Another session tagged v0.6.20 without VERSION on main catching up: never reuse a number."""
    git(world["work"], "tag", "v0.6.20")
    git(world["work"], "push", "-q", "origin", "v0.6.20")
    out = release(world, "--dry-run")
    assert "0.6.20 -> 0.6.21" in out.stdout


def test_a_full_run_bumps_pushes_main_and_the_tag_and_dispatches_the_workflow(world):
    out = release(world, "--yes", "--no-wait")
    assert out.returncode == 0, out.stderr
    assert git(world["origin"], "show", "main:VERSION").stdout == "0.6.15\n"
    assert git(world["origin"], "log", "-1", "--format=%s", "main").stdout.strip() == "release: 0.6.15"
    assert "v0.6.15" in origin_tags(world)
    tag_type = git(world["origin"], "cat-file", "-t", "v0.6.15").stdout.strip()
    assert tag_type == "commit"  # lightweight, the project convention
    assert "workflow run release.yml -f version=0.6.15" in world["log"].read_text()


def test_it_waits_for_the_release_run_unless_told_not_to(world):
    out = release(world, "--yes")
    assert out.returncode == 0, out.stderr
    log = world["log"].read_text()
    assert "run watch 4242 --exit-status" in log


def test_minor_and_explicit_versions(world):
    assert "0.6.14 -> 0.7.0" in release(world, "--dry-run", "--minor").stdout
    assert "0.6.14 -> 1.0.0" in release(world, "--dry-run", "--version", "1.0.0").stdout
    assert release(world, "--dry-run", "--version", "v1").returncode != 0


def test_failing_tests_stop_everything_before_any_push(world):
    out = release(world, "--yes", "--no-wait", tests="exit 1")
    assert out.returncode != 0
    assert "v0.6.15" not in origin_tags(world)
    assert git(world["origin"], "show", "main:VERSION").stdout == "0.6.14\n"
    assert not world["log"].exists()


def test_a_failing_publication_check_stops_before_any_push(world):
    out = release(world, "--yes", "--no-wait", checks="exit 1")
    assert out.returncode != 0 and "v0.6.15" not in origin_tags(world)


def test_main_moving_while_the_checks_run_aborts_and_keeps_the_local_commit(world):
    other = world["tmp"] / "other"
    git(world["tmp"], "clone", "-q", str(world["origin"]), str(other))
    mover = f"cd {other} && echo y > y.txt && git add -A && git commit -q -m other && git push -q origin HEAD:main"
    out = release(world, "--yes", "--no-wait", tests=mover)
    assert out.returncode != 0 and "moved while the checks ran" in out.stderr
    assert "v0.6.15" not in origin_tags(world)
    assert git(world["work"], "log", "-1", "--format=%s").stdout.strip() == "release: 0.6.15"


def test_it_refuses_a_dirty_tree_a_branch_behind_main_and_nothing_to_ship(world):
    (world["work"] / "dirty.txt").write_text("x")
    assert "not clean" in release(world, "--dry-run").stderr
    (world["work"] / "dirty.txt").unlink()

    git(world["work"], "checkout", "-q", "main")
    assert "nothing to release" in release(world, "--dry-run").stderr

    other = world["tmp"] / "other"
    git(world["tmp"], "clone", "-q", str(world["origin"]), str(other))
    (other / "z.txt").write_text("z")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "z")
    git(other, "push", "-q", "origin", "HEAD:main")
    git(world["work"], "checkout", "-q", "feature")
    assert "rebase first" in release(world, "--dry-run").stderr


def test_a_version_that_already_exists_is_refused(world):
    out = release(world, "--dry-run", "--version", "0.6.14")
    assert out.returncode != 0 and "already exists" in out.stderr


def test_the_confirmation_can_decline(world):
    env = {**os.environ, **IDENTITY, "PATH": world["path"], "RELEASE_TEST_CMD": "true", "RELEASE_CHECK_CMD": "true"}
    out = subprocess.run(["bash", "scripts/release.sh", "--no-wait"], cwd=world["work"], env=env, input="n\n",
                         text=True, capture_output=True)
    assert out.returncode != 0 and "v0.6.15" not in origin_tags(world)
