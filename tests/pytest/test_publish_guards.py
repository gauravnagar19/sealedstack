"""Guards from fixes round 3 §A7: metadata identity rule + pre-push hook."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NO_REPLY = "19661833+gauravnagar19@users.noreply.github.com"
ZERO = "0" * 40


def _git(repo: Path, *args: str, check: bool = True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "scripts").mkdir()
    shutil.copy(REPO / "scripts" / "publish-check.sh", repo / "scripts" / "publish-check.sh")
    _git(repo, "init", "-q", "-b", "main")
    (repo / "f.txt").write_text("hello\n")
    return repo


def _commit(repo: Path, name: str, email: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "-q", "-m", "test commit")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _metadata(repo: Path):
    return subprocess.run(["bash", str(repo / "scripts" / "publish-check.sh"), "--metadata"], capture_output=True, text=True)


def test_metadata_fails_on_non_no_reply_identity(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "Someone", "someone@example.com")
    result = _metadata(repo)
    assert result.returncode != 0
    assert "non-neutral" in (result.stdout + result.stderr)


def test_metadata_passes_with_no_reply_identity(tmp_path):
    repo = _make_repo(tmp_path)
    _commit(repo, "gauravnagar19", NO_REPLY)
    result = _metadata(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def _run_hook(repo: Path, stdin: str):
    return subprocess.run(
        ["bash", str(REPO / "scripts" / "hooks" / "pre-push")],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(repo),
    )


def test_hook_refuses_a_disallowed_branch(tmp_path):
    repo = _make_repo(tmp_path)
    head = _commit(repo, "gauravnagar19", NO_REPLY)
    result = _run_hook(repo, f"refs/heads/history-old {head} refs/heads/history-old {ZERO}\n")
    assert result.returncode != 0
    assert "refusing" in result.stderr


def test_hook_allows_main_with_neutral_identity(tmp_path):
    repo = _make_repo(tmp_path)
    head = _commit(repo, "gauravnagar19", NO_REPLY)
    result = _run_hook(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n")
    assert result.returncode == 0, result.stderr


def test_hook_refuses_main_with_personal_email(tmp_path):
    repo = _make_repo(tmp_path)
    head = _commit(repo, "Someone", "someone@example.com")
    result = _run_hook(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n")
    assert result.returncode != 0
    assert "no-reply" in result.stderr
