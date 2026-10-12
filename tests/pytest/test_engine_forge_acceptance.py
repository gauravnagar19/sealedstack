"""M7 acceptance, end-to-end on the in-memory forge (Task 5, no real org)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sealedstack import checklist, forge, statusissue, versionpin


@dataclass
class Result:
    id: str
    status: str
    detail: str = ""
    fix: str = ""


def test_setup_journey_on_a_mocked_forge(tmp_path):
    # A repo pinned to 0.6.0 and calling the reusable workflow.
    (tmp_path / "sealedstack.version").write_text("0.6.0\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "sealedstack.yml").write_text("uses: anomalyco/sealedstack/.github/workflows/infra.yml@v0.6.0\n")
    ok, _ = versionpin.check(tmp_path)
    assert ok

    f = forge.InMemoryForge()
    repo = "you/my-infra"

    # setup: open PR #1 and post the living checklist
    pr = f.open_pr(
        repo,
        branch="sealedstack/setup",
        title="sealedstack setup",
        body="Setup",
        files={"config/stack.yaml": "name: my-infra\n", "config/servers.yaml": "servers: {}\n"},
    )
    f.permissions[(repo, "you")] = "admin"

    first = checklist.from_results(
        [Result("config", "ok", "valid"), Result("secrets.cf", "fail", "not found", fix="create token")]
    )
    comment = f.upsert_comment(repo, pr, checklist.render(first), marker=checklist.MARKER)

    # /sealedstack check re-runs preflight and updates the SAME comment in place
    second = checklist.from_results([Result("config", "ok", "valid"), Result("secrets.cf", "ok", "present")])
    again = f.upsert_comment(repo, pr, checklist.render(second), marker=checklist.MARKER)
    assert again == comment
    assert len(f.comments[(repo, pr)]) == 1
    assert "2 of 2 ready" in f.comments[(repo, pr)][0].body

    # up: publish the pinned Status issue, then a later status update edits it
    issue = statusissue.publish(f, repo, statusissue.render(name="my-infra", version="0.6.0"))
    statusissue.publish(f, repo, statusissue.render(name="my-infra", version="0.6.0", drift="none"))
    assert f.issues[issue]["pinned"] is True
    assert statusissue.issue_number(f, repo) == issue
