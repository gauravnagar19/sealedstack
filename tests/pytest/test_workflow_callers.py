"""D-056: every shown/generated infra caller workflow grants `packages: read`."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load_init():
    spec = importlib.util.spec_from_file_location("sealedstack_init", REPO / "scripts" / "init.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _permissions_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.search(r"permissions:\s*\{", ln)]


def test_generated_infra_workflow_grants_packages_read():
    text = _load_init().infra_workflow("example-owner", "0.6.0")
    lines = _permissions_lines(text)
    assert lines and all("packages: read" in ln for ln in lines)


def test_docs_snippets_grant_packages_read():
    for doc in ("docs/user-experience.md", "docs/getting-started.md"):
        lines = _permissions_lines((REPO / doc).read_text(encoding="utf-8"))
        assert lines, doc
        assert all("packages: read" in ln for ln in lines), doc


def test_ux_status_table_records_the_d056_amendment():
    text = (REPO / "docs" / "user-experience.md").read_text(encoding="utf-8")
    assert "D-056" in text.split("\n---\n", 1)[0]
    assert "### D-056" in (REPO / "docs" / "decisions.md").read_text(encoding="utf-8")
