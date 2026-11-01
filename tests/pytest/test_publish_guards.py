"""Guards from fixes round 3 §A7: metadata identity rule + pre-push hook."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
NO_REPLY = "12345+octocat@users.noreply.github.com"
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
    _commit(repo, "octocat", NO_REPLY)
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
    head = _commit(repo, "octocat", NO_REPLY)
    result = _run_hook(repo, f"refs/heads/history-old {head} refs/heads/history-old {ZERO}\n")
    assert result.returncode != 0
    assert "refusing" in result.stderr


def test_hook_allows_main_with_neutral_identity(tmp_path):
    repo = _make_repo(tmp_path)
    head = _commit(repo, "octocat", NO_REPLY)
    result = _run_hook(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n")
    assert result.returncode == 0, result.stderr


def test_hook_refuses_main_with_personal_email(tmp_path):
    repo = _make_repo(tmp_path)
    head = _commit(repo, "Someone", "someone@example.com")
    result = _run_hook(repo, f"refs/heads/main {head} refs/heads/main {ZERO}\n")
    assert result.returncode != 0
    assert "no-reply" in result.stderr


# -- scripts/gitleaks-history.sh: a scan that could not read the repository must not pass ---------
def _history_scan(tmp_path: Path, stub_body: str, commits: int = 2):
    """Run the wrapper in a throwaway repo with a stub `gitleaks` first on PATH."""
    repo = tmp_path / "h"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for i in range(commits):
        (repo / "f.txt").write_text(f"{i}\n")
        _commit(repo, "octocat", NO_REPLY)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gitleaks"
    stub.write_text("#!/usr/bin/env bash\n" + stub_body)
    stub.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)}
    return subprocess.run(["bash", str(REPO / "scripts" / "gitleaks-history.sh"), str(repo)], capture_output=True, text=True, env=env)


def test_history_scan_passes_when_every_commit_was_scanned(tmp_path):
    result = _history_scan(tmp_path, "echo '2 commits scanned.'; echo 'no leaks found'\n")
    assert result.returncode == 0, result.stdout + result.stderr


def test_history_scan_fails_when_gitleaks_could_not_read_the_repository(tmp_path):
    # What the old CI job logged while still printing "no leaks found" and exiting 0.
    stub = "echo \"ERR [git] fatal: detected dubious ownership\"; echo 'ERR failed to scan Git repository'; echo 'no leaks found'\n"
    result = _history_scan(tmp_path, stub)
    assert result.returncode != 0
    assert "reported an error" in result.stderr


def test_history_scan_fails_when_fewer_commits_were_scanned_than_exist(tmp_path):
    result = _history_scan(tmp_path, "echo '1 commits scanned.'; echo 'no leaks found'\n")
    assert result.returncode != 0
    assert "scanned '1' commits but the repository has 2" in result.stderr


def test_history_scan_fails_when_gitleaks_finds_a_leak(tmp_path):
    result = _history_scan(tmp_path, "echo '2 commits scanned.'; echo 'leaks found: 1'; exit 1\n")
    assert result.returncode != 0


def test_history_scan_trusts_the_checkout_for_git_without_changing_global_config(tmp_path):
    stub = 'echo "$GIT_CONFIG_KEY_0=$GIT_CONFIG_VALUE_0"; echo \'2 commits scanned.\'\n'
    result = _history_scan(tmp_path, stub)
    assert result.returncode == 0
    assert f"safe.directory={tmp_path / 'h'}" in result.stdout


def test_gitleaks_config_only_uses_allowlist_syntax_the_pinned_gitleaks_reads():
    # gitleaks 8.21.2 (toolbox/Dockerfile) silently ignores a global `[[allowlists]]` table (that arrived in
    # 8.25), which made the history scan fail on two reviewed false positives. A rule-level allowlist works.
    import tomllib

    config = tomllib.loads((REPO / ".gitleaks.toml").read_text(encoding="utf-8"))
    assert config["extend"] == {"useDefault": True}
    assert "allowlists" not in config
    (rule,) = config["rules"]
    assert rule["id"] == "generic-api-key"
    (allow,) = rule["allowlists"]
    assert allow["condition"] == "AND" and allow["paths"] and allow["regexes"]
