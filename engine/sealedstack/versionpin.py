"""Version-pin parity (D-031, M7).

The pinned version appears twice in an infra repo: in ``sealedstack.version``
(used by the launcher, Codespaces and GitLab) and in the reusable-workflow ref in
``.github/workflows/sealedstack.yml`` (``uses: <owner>/sealedstack/.github/workflows/infra.yml@vX``),
because GitHub requires a fixed ref. ``check`` fails when they differ, and the
upgrade bot always bumps both in one PR.
"""
from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(".github") / "workflows" / "sealedstack.yml"
_USES_RE = re.compile(r"uses:\s*\S*/sealedstack/\.github/workflows/infra\.yml@(\S+)")


def normalize(ref: str) -> str:
    ref = ref.strip()
    return ref[1:] if ref.startswith("v") else ref


def version_from_file(text: str) -> str:
    return normalize(text.strip())


def ref_from_workflow(text: str) -> str:
    match = _USES_RE.search(text or "")
    return normalize(match.group(1)) if match else ""


def check(code_root: Path, workflow: Path | None = None) -> tuple[bool, str]:
    """Return ``(ok, detail)``. ``ok`` is False when the pins differ."""
    code_root = Path(code_root)
    version_file = code_root / "sealedstack.version"
    workflow_file = Path(workflow) if workflow else code_root / WORKFLOW
    version = version_from_file(version_file.read_text(encoding="utf-8")) if version_file.exists() else ""
    ref = ref_from_workflow(workflow_file.read_text(encoding="utf-8")) if workflow_file.exists() else ""
    if not version:
        return False, "sealedstack.version is missing or empty"
    if not ref:
        return False, f"{WORKFLOW} must pin the reusable workflow ref (uses: .../infra.yml@v<version>)"
    if version != ref:
        return False, f"sealedstack.version ({version}) != workflow ref ({ref}); bump both in one PR"
    return True, f"version pins match ({version})"
