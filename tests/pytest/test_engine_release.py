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
