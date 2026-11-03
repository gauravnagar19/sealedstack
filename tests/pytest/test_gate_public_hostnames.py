"""D-092: gate.public_hostnames — vendor hostnames exempted from the catch-all."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for extra in (REPO / "scripts", REPO / "engine"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import validate  # noqa: E402

SERVERS = {"servers": {"control": {"role": "control"}}}
APPS = {"apps": {"web": {"access": "public", "servers": ["control"], "hostnames": ["app.example.com"]}}}


def stack(public_hostnames):
    return {
        "domain": "example.com",
        "gate": {"policies": {"owner-only": {}}, "catch_all": True, "public_hostnames": public_hostnames},
    }


def test_subdomains_not_owned_by_an_app_pass():
    assert validate.check_cross_file(stack(["clerk.example.com", "accounts.example.com"]), SERVERS, APPS) == []


def test_outside_the_domain_is_rejected():
    problems = validate.check_cross_file(stack(["clerk.other.com", "example.com"]), SERVERS, APPS)
    assert sum("is not a subdomain" in p for p in problems) == 2


def test_an_app_hostname_is_rejected():
    problems = validate.check_cross_file(stack(["app.example.com"]), SERVERS, APPS)
    assert any("belongs to an app" in p for p in problems)


def test_schema_accepts_the_field():
    import json

    schema = json.loads((REPO / "schemas" / "stack.schema.json").read_text())
    assert schema["properties"]["gate"]["properties"]["public_hostnames"]["type"] == "array"
