#!/usr/bin/env python3
"""Keep the compose lint embedded in the app release workflow identical to its source.

``engine/sealedstack/composelint.py`` is the one source of the host-escape rules
(D-063). The reusable app release workflow runs on a bare GitHub runner in an app
repo, where the engine is not installed, so the source is embedded verbatim in a
step of ``.github/workflows/app.yml``. Run this after editing the module:

    python3 scripts/embed_composelint.py          # rewrite the embedded copy
    python3 scripts/embed_composelint.py --check  # exit 1 when they differ

``tests/pytest/test_composelint.py`` runs the check.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = ROOT / "engine" / "sealedstack" / "composelint.py"
WORKFLOW = ROOT / ".github" / "workflows" / "app.yml"
PATTERN = re.compile(r"(          python3 - <<'PY'\n)(.*?)(\n          PY\n)", re.S)
STEP = "Lint the deploy compose for host-escape settings (D-063)"


def embedded_block() -> str:
    lines = SOURCE.read_text(encoding="utf-8").rstrip("\n").split("\n")
    return "\n".join(("          " + line) if line else "" for line in lines)


def current(text: str) -> str:
    start = text.index(f"- name: {STEP}")
    match = PATTERN.search(text, start)
    if not match:
        raise SystemExit("embed: the lint step was not found in app.yml")
    return match.group(2)


def main(argv: list[str]) -> int:
    text = WORKFLOW.read_text(encoding="utf-8")
    want = embedded_block()
    if current(text) == want:
        print("embed: app.yml already carries the current composelint.py")
        return 0
    if "--check" in argv:
        print("embed: app.yml's copy of composelint.py is stale; run python3 scripts/embed_composelint.py", file=sys.stderr)
        return 1
    start = text.index(f"- name: {STEP}")
    match = PATTERN.search(text, start)
    WORKFLOW.write_text(text[: match.start(2)] + want + text[match.end(2):], encoding="utf-8")
    print("embed: updated app.yml")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
