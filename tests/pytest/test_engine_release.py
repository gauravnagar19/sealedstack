"""M8: signed release, dry-run, Renovate, upgrade PR template (Task 6)."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]


def _release() -> dict:
    return yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text())


def test_release_is_manual_and_authorized():
    on = _release().get("on", _release().get(True))
    # manual dispatch only (D-052/B2): no tag trigger
    assert "push" not in on
    assert on["workflow_dispatch"]["inputs"]["dry_run"]["default"] is False
    # the dispatcher must be in the repo-level approvers list
    authorize = _release()["jobs"]["authorize"]["steps"][0]
    assert "RELEASE_APPROVERS" in authorize["env"]["APPROVERS"]


def test_release_pushes_only_after_approval():
    jobs = _release()["jobs"]
    # the real release waits in the `release` environment (owner approval)
    assert jobs["release"]["environment"] == "release"
    release_steps = yaml.safe_dump(jobs["release"]["steps"])
    assert "cosign sign" in release_steps
    assert "sbom" in release_steps.lower()


def test_release_has_a_no_push_dry_run():
    jobs = _release()["jobs"]
    dry = jobs["dry-run"]
    steps = yaml.safe_dump(dry["steps"])
    # builds locally, signs a blob, and never logs in / pushes
    assert "docker save" in steps and "cosign sign-blob" in steps
    assert "push: true" not in steps and "docker/login-action" not in steps
    # the publishing job is skipped when dry_run is set
    assert jobs["release"]["if"].find("dry_run") != -1


def test_renovate_covers_actions_tofu_and_toolbox():
    data = json.loads((REPO / "renovate.json").read_text())
    assert ":dependencyDashboard" in data["extends"]
    managers = " ".join(m for rule in data["packageRules"] for m in rule.get("matchManagers", []))
    assert "github-actions" in managers
    assert "terraform" in managers
    regex = data["regexManagers"]
    assert any(rule["depNameTemplate"] == "opentofu/opentofu" for rule in regex)
    assert any(rule["datasourceTemplate"] == "pypi" for rule in regex)
    assert data["terraform"]["enabled"] is True


def test_upgrade_pr_template_exists():
    text = (REPO / "examples" / "upgrade-pr-template.md").read_text()
    assert "sealedstack.version" in text
    assert "Roll back" in text


def test_no_legacy_tailscale_api_key_anywhere():
    import glob

    hits = []
    for pattern in ("engine/sealedstack/*.py", ".github/workflows/*.yml"):
        for path in glob.glob(str(REPO / pattern)):
            text = Path(path).read_text()
            if "TAILSCALE_API_KEY" in text:
                hits.append(path)
    assert hits == [], f"TAILSCALE_API_KEY still present in {hits}"


# -- D-082: the release waits for, and requires, a green ci run for the dispatched commit ---------
def _gate_script() -> str:
    step = _release()["jobs"]["ci-gate"]["steps"][0]
    return step["run"]


def test_release_builds_nothing_until_ci_has_succeeded_for_the_commit():
    jobs = _release()["jobs"]
    gate = jobs["ci-gate"]
    assert gate["permissions"] == {"actions": "read", "contents": "read"}
    assert "authorize" in gate["needs"] and "dry_run" in gate["if"]
    assert gate["steps"][0]["env"]["SHA"] == "${{ github.sha }}"
    # everything that builds or pushes is downstream of the gate, and runs only if it succeeded
    assert "ci-gate" in jobs["toolbox"]["needs"]
    release = jobs["release"]
    assert "ci-gate" in release["needs"] and "toolbox" in release["needs"]
    assert "needs.ci-gate.result == 'success'" in release["if"] and "needs.toolbox.result == 'success'" in release["if"]
    # a bare always() would run the push after a failed gate
    assert "always()" not in release["if"]
    # the dry run pushes nothing and is not gated
    assert "ci-gate" not in jobs["dry-run"].get("needs", [])


def test_the_gate_reads_the_ci_workflow_runs_of_exactly_this_commit_through_env():
    script = _gate_script()
    assert "actions/workflows/ci.yml/runs?head_sha=${SHA}" in script
    assert "${{" not in script  # no expression is interpolated into the shell


def _run_gate(tmp_path: Path, answers: list[str]):
    """Run the gate with a stub `gh` that answers from a list (one answer per poll) and a no-op sleep."""
    import os
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    answers_file = tmp_path / "answers"
    answers_file.write_text("\n".join(answers) + "\n")
    counter = tmp_path / "n"
    counter.write_text("0")
    (bin_dir / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f'n=$(cat "{counter}"); echo $((n+1)) > "{counter}"\n'
        f'line=$(sed -n "$((n+1))p" "{answers_file}")\n'
        f'[ -n "$line" ] || line=$(tail -n 1 "{answers_file}")\n'
        'if [ "$line" = "ERROR" ]; then echo "gh: HTTP 403" >&2; exit 1; fi\n'
        'printf "%b" "$line"\n'
    )
    (bin_dir / "sleep").write_text("#!/usr/bin/env bash\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    # SECONDS cannot be faked from outside, so the 5-minute wait for a first run is shortened by editing the script text.
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "REPO": "o/r", "SHA": "abc123", "GH_TOKEN": "x"}
    script = _gate_script().replace("-ge 300", "-ge -1")  # deadline reached at once: "no run" fails on the first empty poll
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def test_the_gate_passes_on_a_successful_run(tmp_path):
    result = _run_gate(tmp_path, ["completed\\tsuccess\\thttps://example/run/1"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ci succeeded for abc123" in result.stdout


def test_the_gate_waits_for_an_unfinished_run_then_passes(tmp_path):
    result = _run_gate(tmp_path, ["in_progress\\tnone\\thttps://example/run/1", "completed\\tsuccess\\thttps://example/run/1"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ci is in_progress for abc123" in result.stdout


def test_the_gate_refuses_a_red_run_and_says_which(tmp_path):
    for conclusion in ("failure", "cancelled", "skipped", "timed_out"):
        (tmp_path / conclusion).mkdir()
        result = _run_gate(tmp_path / conclusion, [f"completed\\t{conclusion}\\thttps://example/run/2"])
        assert result.returncode == 1
        assert f"ci finished '{conclusion}' for abc123" in result.stdout and "https://example/run/2" in result.stdout


def test_the_gate_refuses_when_there_is_no_ci_run_for_the_commit(tmp_path):
    result = _run_gate(tmp_path, [""])
    assert result.returncode == 1
    assert "no ci run found for abc123" in result.stdout


def test_the_gate_fails_closed_when_the_api_call_fails(tmp_path):
    result = _run_gate(tmp_path, ["ERROR"])
    assert result.returncode != 0 and "ci succeeded" not in result.stdout


def test_the_gate_query_picks_the_newest_run_and_ignores_none(tmp_path):
    import shutil
    import subprocess

    if shutil.which("jq") is None:
        import pytest

        pytest.skip("jq is not installed here; the runner has it")
    script = _gate_script()
    jq = script.split("--jq '", 1)[1].split("')\"", 1)[0]
    runs = {
        "workflow_runs": [
            {"created_at": "2026-01-01T00:00:00Z", "status": "completed", "conclusion": "failure", "html_url": "old"},
            {"created_at": "2026-01-01T00:05:00Z", "status": "in_progress", "conclusion": None, "html_url": "new"},
        ]
    }
    out = subprocess.run(["jq", "-r", jq], input=json.dumps(runs), capture_output=True, text=True, check=True).stdout
    assert out == "in_progress\tnone\tnew\n"
    empty = subprocess.run(["jq", "-r", jq], input='{"workflow_runs": []}', capture_output=True, text=True, check=True).stdout
    assert empty == ""
