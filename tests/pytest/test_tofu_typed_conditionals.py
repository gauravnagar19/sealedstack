"""A `for_each` over a map of objects must not be a `cond ? map : {}` conditional (live run 2026-11-01).

OpenTofu requires both branches of a conditional to have the same type. A map of objects whose
members differ (two apps with different attributes, for example) is an object type with one
attribute per app, which `{}` never matches: the plan failed as soon as `demo-app` joined `hello`.
A for expression with an `if` filter has no such type constraint.
"""
from __future__ import annotations

import re

from test_engine_server import REPO

# `? local.<name> : {}` after for_each; the `local.on`/`local.off` pairs are plain maps and stay fine.
TYPED = re.compile(r"for_each\s*=.*\?\s*local\.(?!on\b)\w+\s*:\s*\{\}")


def test_no_stack_picks_an_app_map_with_a_typed_conditional():
    offenders = []
    for path in sorted((REPO / "tofu").rglob("*.tf")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.lstrip().startswith("#") and TYPED.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, "use `{ for k, v in MAP : k => v if COND }` instead:\n" + "\n".join(offenders)


def test_no_count_or_for_each_depends_on_a_destination_id():
    """Live run 2026-11-01: the first database app failed `Invalid count argument` because `count` compared a destination id
    that only exists after the destination is created. Gate on the plan-time booleans instead."""
    module = (REPO / "tofu" / "modules" / "platform_dokploy" / "main.tf").read_text(encoding="utf-8")
    code = "\n".join(line for line in module.splitlines() if not line.lstrip().startswith("#"))
    assert 'destination_id != ""' not in code
    assert "var.db_backup_enabled" in code and "var.secondary_backup_enabled" in code
