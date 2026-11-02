"""``state.prefix`` decouples the R2 namespace from the infra name, so a renamed infra keeps its state."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sealedstack import config as cfg, drillrecord, lock  # noqa: E402
from test_infra_ci import REPO  # noqa: E402


def load(tmp_path, **state):
    cdir = tmp_path / "config"
    cdir.mkdir(exist_ok=True)
    stack = {"name": "paisa-infra", "domain": "example.com", "state": {"backend": "r2", "bucket": "bkt", **state}}
    (cdir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {}}))
    (cdir / "apps.yaml").write_text(yaml.safe_dump({"apps": {}}))
    return cfg.load_config(cdir, REPO, {})


def test_the_prefix_defaults_to_the_name(tmp_path):
    assert load(tmp_path).state_prefix == "paisa-infra"


def test_an_explicit_prefix_wins_for_state_lock_and_drill_results(tmp_path):
    c = load(tmp_path, prefix="demo-infra")
    assert c.name == "paisa-infra" and c.state_prefix == "demo-infra"
    assert lock.lock_key(c.state_prefix) == "sealedstack/demo-infra/state.lock"
    assert drillrecord.record_key(c.state_prefix).startswith("sealedstack/demo-infra/")


def test_the_schema_accepts_a_prefix_and_rejects_a_bad_one(tmp_path):
    import json
    import jsonschema

    schema = json.loads((REPO / "schemas" / "stack.schema.json").read_text())
    base = yaml.safe_load((tmp_path / "x").read_text()) if False else None  # noqa: F841
    stack = {"name": "a", "domain": "example.com", "state": {"backend": "r2", "bucket": "bkt", "prefix": "demo-infra"}}
    errors = lambda s: list(jsonschema.Draft202012Validator(schema).iter_errors(s))  # noqa: E731
    assert not [e for e in errors(stack) if "prefix" in str(e.path)]
    stack["state"]["prefix"] = "Bad Prefix"
    assert [e for e in errors(stack) if "prefix" in str(e.path)]
