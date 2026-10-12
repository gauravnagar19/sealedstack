"""M7: forge adapters, version pin, checklist, status issue, commands (Task 5)."""
from __future__ import annotations

from pathlib import Path

import yaml

from sealedstack import botmanifest, checklist, commands, forge, statusissue, versionpin

REPO = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# version-pin parity
# --------------------------------------------------------------------------- #
def test_version_pin_matches(tmp_path):
    (tmp_path / "sealedstack.version").write_text("0.6.0\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "sealedstack.yml").write_text("uses: anomalyco/sealedstack/.github/workflows/infra.yml@v0.6.0\n")
    ok, detail = versionpin.check(tmp_path)
    assert ok and "0.6.0" in detail


def test_version_pin_mismatch_fails(tmp_path):
    (tmp_path / "sealedstack.version").write_text("0.6.0\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "sealedstack.yml").write_text("uses: anomalyco/sealedstack/.github/workflows/infra.yml@v0.5.0\n")
    ok, detail = versionpin.check(tmp_path)
    assert not ok and "0.6.0" in detail and "0.5.0" in detail


def test_version_pin_missing_ref_fails(tmp_path):
    (tmp_path / "sealedstack.version").write_text("0.6.0\n")
    ok, detail = versionpin.check(tmp_path)
    assert not ok and "infra.yml@" in detail


# --------------------------------------------------------------------------- #
# checklist
# --------------------------------------------------------------------------- #
def test_checklist_renders_progress_and_marker():
    items = [
        checklist.Item("ok", "Config files", "valid"),
        checklist.Item("fail", "CLOUDFLARE_API_TOKEN", "not found", fix="create token"),
        checklist.Item("skip", "Optional: Clerk"),
    ]
    text = checklist.render(items)
    assert checklist.MARKER in text
    assert "1 of 2 ready" in text  # a skip is not counted
    assert "❌" in text and "✅" in text
    assert "create token" in text


# --------------------------------------------------------------------------- #
# status issue
# --------------------------------------------------------------------------- #
def test_status_publish_creates_then_updates_pinned_issue():
    f = forge.InMemoryForge()
    body = statusissue.render(name="my-infra", version="0.6.0", servers=[{"name": "server-1"}])
    number = statusissue.publish(f, "me/my-infra", body)
    assert f.issues[number]["pinned"] is True
    assert statusissue.issue_number(f, "me/my-infra") == number
    statusissue.publish(f, "me/my-infra", body + "\nupdated")
    assert statusissue.issue_number(f, "me/my-infra") == number  # same issue
    assert "updated" in f.issues[number]["body"]


def test_status_problem_posts_a_comment():
    f = forge.InMemoryForge()
    number = statusissue.publish(f, "me/my-infra", statusissue.render(name="my-infra", version="0.6.0"))
    statusissue.post_problem(f, "me/my-infra", "Backup older than 2 intervals")
    bodies = [c.body for c in f.comments[("me/my-infra", number)]]
    assert any("Backup older" in b for b in bodies)


# --------------------------------------------------------------------------- #
# forge contract
# --------------------------------------------------------------------------- #
def test_forge_upsert_comment_updates_in_place():
    f = forge.InMemoryForge()
    first = f.upsert_comment("me/repo", 1, "v1\n" + checklist.MARKER, marker=checklist.MARKER)
    second = f.upsert_comment("me/repo", 1, "v2\n" + checklist.MARKER, marker=checklist.MARKER)
    assert first == second
    assert len(f.comments[("me/repo", 1)]) == 1
    assert f.comments[("me/repo", 1)][0].body.startswith("v2")


def test_forge_open_pr_and_variables():
    f = forge.InMemoryForge()
    number = f.open_pr("me/repo", branch="sealedstack/setup", title="sealedstack setup", body="checklist", files={"config/stack.yaml": "x"})
    assert number and f.files[("me/repo", "sealedstack/setup", "config/stack.yaml")] == "x"
    f.set_variable("you/shop", "SEALEDSTACK_REGISTRY", "ghcr.io/you")
    assert f.get_variable("you/shop", "SEALEDSTACK_REGISTRY") == "ghcr.io/you"
    f.set_secret("you/shop", "DOKPLOY_APP_ID", "abc", environment="production")
    assert f.secrets[("you/shop", "production", "DOKPLOY_APP_ID")] == "abc"


# --------------------------------------------------------------------------- #
# comment commands
# --------------------------------------------------------------------------- #
def test_comment_command_parses():
    parsed = commands.parse("/sealedstack check now")
    assert parsed and parsed.command == "check"
    assert commands.parse("hello") is None
    assert commands.parse("/sealedstack bogus") is None


def test_comment_command_gates_writers():
    f = forge.InMemoryForge()
    f.permissions[("me/repo", "alice")] = "write"
    f.permissions[("me/repo", "mallory")] = "read"

    allowed = commands.handle(f, "me/repo", 5, "/sealedstack up", "alice")
    assert allowed and allowed.command == "up"

    refused = commands.handle(f, "me/repo", 5, "/sealedstack up", "mallory")
    assert refused is None
    assert any("write access" in c.body for c in f.comments[("me/repo", 5)])


# --------------------------------------------------------------------------- #
# bot manifest
# --------------------------------------------------------------------------- #
def test_bot_manifest_permissions_and_form():
    manifest = botmanifest.manifest("sealedstack-bot", "https://sealedstack.dev", "https://example.com/cb")
    assert manifest["default_permissions"]["contents"] == "write"
    assert manifest["default_permissions"]["issues"] == "write"
    assert "pull_request" in manifest["default_events"]
    html = botmanifest.form_html("sealedstack-bot", "https://sealedstack.dev", "https://example.com/cb")
    assert 'action="https://github.com/settings/apps/new"' in html
    assert "sealedstack-bot" in html


# --------------------------------------------------------------------------- #
# the reusable workflow
# --------------------------------------------------------------------------- #
def test_infra_workflow_shape():
    data = yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text())
    # PyYAML parses the bare `on:` key as the boolean True.
    triggers = data.get("on", data.get(True))
    call = triggers["workflow_call"]
    assert "command" in call["inputs"]
    assert "args" in call["inputs"]
    assert data["permissions"]["contents"] == "write"
    assert data["permissions"]["pull-requests"] == "write"
    assert data["permissions"]["issues"] == "write"
    # writes wait for approval in the production environment
    approve = data["jobs"]["approve"]
    assert approve["environment"] == "production"
    # comment commands are gated by collaborator permission
    decide = data["jobs"]["authorize"]["steps"][0]
    assert "collaborators" in decide["run"] and "write" in decide["run"]
