#!/usr/bin/env python3
"""Thin wrapper so orchestration can call `python3 scripts/status.py` (spec 6.4).

Puts the engine package on the path and delegates to it.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))

from sealedstack import status  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(status.main())
