#!/usr/bin/env python3
"""Resolve the user-specific config directory (spec D-020).

The config can live outside this repository, in a private ``sealedstack-config``
repo. The single source of truth is the ``SEALEDSTACK_CONFIG`` environment
variable; it defaults to ``<repo>/config`` so the original single-repo layout
keeps working unchanged.
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def config_dir() -> pathlib.Path:
    """Return the config directory.

    ``SEALEDSTACK_CONFIG`` wins when set; otherwise ``<repo>/config``.
    """
    override = os.environ.get("SEALEDSTACK_CONFIG")
    if override:
        return pathlib.Path(override).expanduser()
    return ROOT / "config"
