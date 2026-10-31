"""Docs guard: decisions are numbered once, and the planned D-065 stays visible (no code behind it yet)."""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DECISIONS = (REPO / "docs" / "maintainers" / "decisions.md").read_text(encoding="utf-8")


def test_decision_ids_are_unique():
    ids = re.findall(r"^### (D-\d{3}) ", DECISIONS, re.M)
    assert len(ids) == len(set(ids)) and "D-065" in ids


def test_d065_is_recorded_as_planned_with_its_rationale():
    section = DECISIONS.split("### D-065", 1)[1].split("\n### ", 1)[0]
    assert "planned" in section.splitlines()[0] and "2026-10-31" in section.splitlines()[0]
    for phrase in ("hold **no** Dokploy credential", "tag:ci", "FR-1", "D-063", "after M4", "write access to an app repo"):
        assert phrase in section, phrase


def test_the_limitations_page_points_at_d065():
    text = (REPO / "docs" / "limitations.md").read_text(encoding="utf-8")
    assert text.count("D-065") >= 2 and "Planned: D-065" in text


def test_d065_is_planned_only_no_workflow_or_engine_code_implements_it():
    for rel in (".github/workflows/app.yml", ".github/workflows/infra.yml"):
        assert "D-065" not in (REPO / rel).read_text(encoding="utf-8")
