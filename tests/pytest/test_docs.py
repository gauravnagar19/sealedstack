"""Guards for the public documentation (CI-first UX, D-034 / D-042 / D-045).

* The public docs must not describe the old laptop + Docker + ``just`` flow.
* Every relative link in the public docs must resolve (file and anchor).
* The golden-path providers named in the README exist in the registry.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

# Maintainer docs (docs/maintainers/) may talk about `just` and superseded designs;
# everything else under docs/ is user-facing (D-069).
MAINTAINERS = REPO / "docs" / "maintainers"

FORBIDDEN = [
    "brew install just",
    "on your laptop",
    "just server-up",
    "just apply",
    "export ROOT_PASSWORD",
]


def public_docs() -> list[Path]:
    files = [REPO / "README.md"]
    files += sorted(
        p for p in (REPO / "docs").rglob("*.md") if MAINTAINERS not in p.parents
    )
    return files


def rel(path: Path, root: Path = REPO) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# forbidden phrases
# --------------------------------------------------------------------------- #
def find_forbidden(text: str) -> list[str]:
    lowered = text.lower()
    return [phrase for phrase in FORBIDDEN if phrase.lower() in lowered]


@pytest.mark.parametrize("path", public_docs(), ids=rel)
def test_public_docs_do_not_describe_the_laptop_flow(path):
    hits = find_forbidden(path.read_text(encoding="utf-8"))
    assert not hits, f"{rel(path)} contains outdated laptop-flow wording: {hits}"


def test_forbidden_phrase_detector_catches_each_phrase():
    for phrase in FORBIDDEN:
        assert find_forbidden(f"Run this: {phrase.upper()} now") == [phrase]
    assert find_forbidden("Open the Actions tab and run `setup`.") == []


def test_public_docs_scope_excludes_contributor_docs():
    names = {p.name for p in public_docs()}
    assert "README.md" in names and "getting-started.md" in names
    assert "development.md" not in names and "decisions.md" not in names and "spec.md" not in names


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #
LINK = re.compile(r"(?<!\!)\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def strip_code(text: str) -> str:
    """Drop fenced code blocks and inline code so example snippets aren't links."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(re.sub(r"`[^`]*`", "", line))
    return "\n".join(out)


def slug(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # [text](url) -> text
    text = re.sub(r"[*_]{1,2}(\S.*?\S|\S)[*_]{1,2}", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"\s", "-", text)


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        n = counts.get(base, 0)
        counts[base] = n + 1
        found.add(base if n == 0 else f"{base}-{n}")
    return found


def broken_links(path: Path, root: Path = REPO) -> list[str]:
    problems = []
    for target in LINK.findall(strip_code(path.read_text(encoding="utf-8"))):
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
            continue  # http(s):, mailto:, ...
        file_part, _, fragment = target.partition("#")
        dest = path if not file_part else (path.parent / file_part).resolve()
        if not dest.exists():
            problems.append(f"{target}: {rel(dest, root)} does not exist")
            continue
        if fragment and dest.suffix == ".md":
            if fragment not in anchors(dest):
                problems.append(f"{target}: no heading with anchor #{fragment} in {rel(dest, root)}")
    return problems


@pytest.mark.parametrize("path", public_docs(), ids=rel)
def test_public_doc_links_resolve(path):
    assert broken_links(path) == []


def test_link_checker_catches_broken_file_and_anchor(tmp_path):
    page = tmp_path / "a.md"
    other = tmp_path / "b.md"
    other.write_text("# Title\n\n## Step 3b: the thing (D-050)\n", encoding="utf-8")
    page.write_text(
        "[ok](b.md) [ok2](b.md#step-3b-the-thing-d-050) [bad file](nope.md)"
        " [bad anchor](b.md#missing) [web](https://example.com) `[code](skip.md)`\n"
        "```\n[fenced](skip2.md)\n```\n",
        encoding="utf-8",
    )
    problems = broken_links(page, tmp_path)
    assert len(problems) == 2
    assert any("nope.md" in p for p in problems)
    assert any("#missing" in p for p in problems)


def test_no_public_doc_links_into_internal():
    for path in public_docs():
        text = path.read_text(encoding="utf-8")
        assert "](internal/" not in text and "](../internal/" not in text, rel(path)


# --------------------------------------------------------------------------- #
# README provider table vs the registry
# --------------------------------------------------------------------------- #
GOLDEN = {
    "compute": ["any-ssh", "netcup", "hetzner"],
    "mesh": ["tailscale"],
    "ingress": ["cloudflare"],
    "gate": ["cloudflare_access"],
    "secrets": ["bitwarden"],
    "backup": ["r2"],
    "platform": ["dokploy"],
    "registry": ["ghcr"],
    "user_auth": ["clerk"],
    "forge": ["github"],
}


def registry_entries() -> dict[str, dict[str, dict]]:
    out = {}
    for path in (REPO / "registry").glob("*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        out[data["slot"]] = {impl["id"]: impl for impl in data["implementations"]}
    return out


def test_golden_path_providers_exist_and_none_is_supported_yet():
    registry = registry_entries()
    for slot, ids in GOLDEN.items():
        for impl in ids:
            assert impl in registry[slot], f"{slot}/{impl} missing from the registry"
    supported = [
        f"{slot}/{impl}"
        for slot, impls in registry.items()
        for impl, entry in impls.items()
        if entry.get("status") == "supported"
    ]
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    if not supported:
        # The README must say so, and must not claim anything is supported.
        assert "Nothing is `supported` until" in readme


def test_readme_lists_golden_path_before_experimental():
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    golden = readme.index("### Golden path")
    experimental = readme.index("### Experimental: implemented, never run live")
    assert golden < experimental
    for ids in GOLDEN.values():
        for impl in ids:
            assert impl in readme[golden:experimental], impl


# --------------------------------------------------------------------------- #
# user-facing messages in the code use the sealedstack vocabulary, not `just`
# --------------------------------------------------------------------------- #
def test_runtime_messages_do_not_tell_users_to_run_just_or_init():
    offenders = []
    for tree in ("engine", "scripts"):
        for path in sorted((REPO / tree).rglob("*")):
            if not path.is_file() or path.suffix not in {".py", ".sh"} or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"`just |'just |\"just |  just (apply|app-restore|state-init|doctor|validate)|sealedstack init|bin/sealedstack validate", line):
                    offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert offenders == []


# --------------------------------------------------------------------------- #
# audiences (D-069)
# --------------------------------------------------------------------------- #
def maintainer_docs() -> list[Path]:
    return sorted(MAINTAINERS.glob("*.md"))


def every_doc() -> list[Path]:
    return public_docs() + maintainer_docs() + [REPO / n for n in ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md")]


@pytest.mark.parametrize("path", maintainer_docs(), ids=rel)
def test_maintainer_docs_say_they_are_not_setup_guidance(path):
    head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:6])
    assert "**Maintainers.**" in head, rel(path)


@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"])
def test_root_contributor_files_point_users_elsewhere(name):
    head = "\n".join((REPO / name).read_text(encoding="utf-8").splitlines()[:6])
    assert "run-with-an-agent.md" in head and ("changing sealedstack itself" in head), name


@pytest.mark.parametrize("path", every_doc(), ids=rel)
def test_every_doc_link_resolves_including_maintainers(path):
    assert broken_links(path) == []


def test_the_design_docs_are_not_in_the_user_docs_folder():
    top = {p.name for p in (REPO / "docs").glob("*.md")}
    assert not top & {"spec.md", "architecture.md", "decisions.md", "development.md"}
    assert {p.name for p in maintainer_docs()} >= {"spec.md", "architecture.md", "decisions.md", "development.md", "adding-a-provider.md"}


def test_user_docs_never_tell_the_reader_to_follow_a_maintainer_page():
    # a user page may cite a decision (D-0nn) or a design section for the "why"; it must not
    # send the reader there to DO something. The adding-a-provider recipe is the one pointer.
    for path in public_docs():
        for target in LINK.findall(strip_code(path.read_text(encoding="utf-8"))):
            if "maintainers/" in target:
                assert target.split("#")[0].endswith(
                    ("decisions.md", "spec.md", "architecture.md", "adding-a-provider.md", "README.md")
                ), f"{rel(path)} links {target}"


def test_the_docs_site_keeps_maintainer_pages_in_their_own_section():
    nav = (REPO / "mkdocs.yml").read_text(encoding="utf-8")
    section = nav.split("Maintainers (not needed to use sealedstack):", 1)[1].split("markdown_extensions:", 1)[0]
    for name in ("architecture", "spec", "decisions", "development", "adding-a-provider"):
        assert f"maintainers/{name}.md" in section
    before = nav.split("Maintainers (not needed to use sealedstack):", 1)[0]
    assert "maintainers/" not in before


def test_d069_is_recorded():
    text = (MAINTAINERS / "decisions.md").read_text(encoding="utf-8")
    assert "### D-069" in text and "split by audience" in text
