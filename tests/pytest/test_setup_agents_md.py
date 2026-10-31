"""Setup writes AGENTS.md (and CLAUDE.md) into the infra repo (D-068).

The file carries the rules an AI coding agent must follow in the infra repo. It holds
names and process only, never a value, an address or a personal detail.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import infraci, scaffold, setupjob  # noqa: E402

BASE = ["--domain", "example.com", "--email", "you@example.com", "--server", "server-1"]


def files_for(*extra, owner="you"):
    args = setupjob.parse_args([*BASE, *extra])
    return setupjob.build_files(args, repo="you/my-infra", version="0.6.0", owner=owner)


def test_setup_adds_agents_md_and_a_claude_md_that_points_at_it():
    files = files_for()
    assert files["AGENTS.md"].startswith("# AGENTS.md: working in my-infra")
    assert files["CLAUDE.md"] == "@AGENTS.md\n"


def test_the_rules_an_agent_must_follow_are_all_there():
    text = files_for()["AGENTS.md"]
    for needle in (
        "only through pull requests",
        "--break-glass",
        "never see secret values",
        "merges PR #1 and approves `up`",
        "Do not create accounts, tokens or API keys",
        "One change at a time",
        "/sealedstack check",
        "/sealedstack drift",
        "gh workflow run sealedstack.yml -f command=up",
        "Status** issue",
    ):
        assert needle in text, needle


def test_the_commands_it_names_exist_in_the_caller_workflow():
    from sealedstack import cli  # noqa: F401  (the workflow options come from docs, asserted below)

    docs = (REPO / "docs" / "getting-started.md").read_text(encoding="utf-8")
    options = re.search(r"options: \[([^\]]+)\]", docs).group(1).split(", ")
    text = files_for()["AGENTS.md"]
    for command in re.findall(r"-f command=([a-z-]+)", text):
        assert command in options, command


@pytest.mark.parametrize(
    "extra, present, absent",
    [
        ((), "BWS_ACCESS_TOKEN", "DOPPLER_TOKEN"),
        (("--secrets", "doppler"), "DOPPLER_TOKEN", "BWS_ACCESS_TOKEN"),
        (("--secrets", "infisical"), "INFISICAL_CLIENT_ID", "BWS_ACCESS_TOKEN"),
    ],
)
def test_the_secret_source_section_names_the_right_credentials(extra, present, absent):
    text = files_for(*extra)["AGENTS.md"]
    assert present in text and absent not in text


def test_infisical_tells_the_agent_to_fill_in_the_project_id():
    assert "providers.secrets_infisical.project_id" in files_for("--secrets", "infisical")["AGENTS.md"]


def test_other_sources_do_not_claim_credentials_they_do_not_have():
    text = files_for("--secrets", "sops")["AGENTS.md"]
    assert "**sops**" in text and "BWS_ACCESS_TOKEN" not in text


def test_it_links_the_owners_sealedstack_repo_and_never_a_placeholder_owner():
    assert "github.com/you/sealedstack" in files_for()["AGENTS.md"]
    bare = files_for(owner="")["AGENTS.md"]
    assert "github.com/OWNER/sealedstack" in bare  # the intro keeps a visible placeholder ...
    assert "<https://github.com/" not in bare  # ... and the docs line makes no link


def test_it_holds_no_ip_address_secret_or_personal_value():
    text = files_for()["AGENTS.md"] + files_for("--secrets", "infisical")["AGENTS.md"]
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text)
    assert "you@example.com" not in text and not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)  # no e-mail address
    assert not re.search(r"(dp\.st\.|ghp_|github_pat_|sk-)", text)


def test_the_readme_lists_the_new_files():
    assert "`AGENTS.md`" in files_for()["README.md"]


def test_editing_agents_md_never_triggers_an_apply():
    assert not any("AGENTS.md".startswith(p) or "CLAUDE.md".startswith(p) for p in infraci.RELEVANT_PREFIXES)


def test_the_documented_file_list_matches_what_setup_writes():
    docs = (REPO / "docs" / "getting-started.md").read_text(encoding="utf-8")
    assert "AGENTS.md" in docs
    assert "AGENTS.md" in (REPO / "docs" / "user-experience.md").read_text(encoding="utf-8")


def test_d068_is_recorded():
    text = (REPO / "docs" / "decisions.md").read_text(encoding="utf-8")
    assert "### D-068" in text and "AGENTS.md" in text.split("### D-068", 1)[1]
