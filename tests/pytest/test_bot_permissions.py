"""D-058: the bot token's permission list is derived from the forge calls."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import botpermissions  # noqa: E402


def forge_calls() -> set[tuple[str, str]]:
    """Every ``self._request("VERB", f"...")`` in GitHubForge, normalised."""
    tree = ast.parse((REPO / "engine" / "sealedstack" / "forge.py").read_text(encoding="utf-8"))
    github = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "GitHubForge")
    calls = set()
    for node in ast.walk(github):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_request"):
            continue
        method, path = node.args[0], node.args[1]
        assert isinstance(method, ast.Constant), "the HTTP method must be a literal"
        if isinstance(path, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in path.values)
        else:
            assert isinstance(path, ast.Constant)
            text = path.value
        calls.add((method.value, botpermissions.normalise(text)))
    return calls


def test_every_forge_call_is_mapped_to_a_permission_and_nothing_extra_is_listed():
    calls = forge_calls()
    assert calls, "found no forge calls: the parser needs updating"
    unmapped = sorted(c for c in calls if c not in botpermissions.CALLS)
    assert not unmapped, f"add these forge calls to botpermissions.CALLS: {unmapped}"
    stale = sorted(k for k in botpermissions.CALLS if k not in calls)
    assert not stale, f"botpermissions.CALLS lists calls the forge no longer makes: {stale}"


def test_the_required_permissions_are_exactly_what_the_calls_need():
    needed: dict[str, str] = {}
    for permission, access in botpermissions.CALLS.values():
        if access == "write" or permission not in needed:
            needed[permission] = access
    listed = {p.name: p.access for p in botpermissions.PERMISSIONS}
    # `Workflows` is needed by file writes under .github/workflows (not a separate endpoint)
    # and `Administration` only by environment creation, which is optional.
    assert listed.pop("Workflows") == "write"
    assert needed.pop("Administration") == "write" and listed.pop("Administration") == "write"
    assert listed == needed
    assert [p.name for p in botpermissions.optional()] == ["Administration"]


def test_the_checked_workflow_paths_really_need_the_workflows_permission():
    # the engine writes workflow files in PRs (onboarding + version bumps)
    onboard = (REPO / "engine" / "sealedstack" / "onboard.py").read_text(encoding="utf-8")
    assert ".github/workflows/release.yml" in onboard
    versionpin = (REPO / "engine" / "sealedstack" / "versionpin.py").read_text(encoding="utf-8")
    assert "sealedstack.yml" in versionpin


def test_repository_secrets_are_requested_because_the_dokploy_key_is_a_repository_secret():
    """FR-2: environment secrets are unavailable on private Free repos and invisible to a job
    without `environment:`, so the key is a repository secret (Secrets, not Environments)."""
    assert "Secrets" in {p.name for p in botpermissions.PERMISSIONS}
    assert "Environments" not in {p.name for p in botpermissions.PERMISSIONS}
    secret_calls = [k for k in botpermissions.CALLS if "/actions/secrets" in k[1]]
    assert len(secret_calls) == 3 and all(botpermissions.CALLS[k][0] == "Secrets" for k in secret_calls)


def test_table_and_short_list_render_the_same_permissions():
    table = botpermissions.table_markdown()
    for p in botpermissions.PERMISSIONS:
        assert p.name in table
    short = botpermissions.short_list()
    assert "Contents: read and write" in short and "Metadata: read" in short and "Administration" not in short
    assert "Administration" in botpermissions.short_list(include_optional=True)


def test_token_kinds():
    assert botpermissions.token_kind("github_pat_abc") == "fine-grained"
    assert botpermissions.token_kind("ghp_abc") == "classic"
    assert botpermissions.token_kind("ghs_abc") == "builtin"
    assert botpermissions.token_kind("gho_abc") == "oauth"
    assert botpermissions.token_kind("zzz") == "unknown"


def test_the_prerequisites_page_shows_the_same_permission_table():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    assert botpermissions.table_markdown() in text
    assert "GH_TOKEN" in text and "fine-grained" in text


def test_the_ux_checklist_row_and_getting_started_use_the_token_not_the_app():
    ux = (REPO / "docs" / "user-experience.md").read_text(encoding="utf-8")
    assert "fine-grained token `GH_TOKEN`" in ux and "D-058 amendment" in ux
    started = (REPO / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "Create the token" in started and "register a GitHub App" not in started
