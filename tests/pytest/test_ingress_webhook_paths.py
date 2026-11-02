"""Webhook paths get a tunnel rule without the Access check, ahead of the gated hostname's rule (D-090).

paisa on 0.6.39: the Bypass Access apps for `user_auth.webhook_paths` existed, but the only tunnel rule for the hostname
set `originRequest.access.required`, so cloudflared answered every webhook with a bare 403. These tests evaluate the
module's own `locals` block with `tofu console` (no provider, no network) and look at the ingress list it builds.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from test_engine_server import REPO

MODULE = REPO / "tofu" / "modules" / "ingress_cloudflare"
TRAEFIK = "http://dokploy-traefik:80"

pytestmark = pytest.mark.skipif(shutil.which("tofu") is None, reason="needs tofu (run in the sealedstack-tests image)")


def locals_block() -> str:
    text = (MODULE / "main.tf").read_text(encoding="utf-8")
    start = text.index("\nlocals {\n") + 1
    return text[start:text.index("\n}\n", start) + 3]


def ingress(tmp_path, **values) -> list[dict]:
    """The module's `local.ingress` for these variable values."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    shutil.copy(MODULE / "variables.tf", tmp_path / "variables.tf")
    (tmp_path / "main.tf").write_text(locals_block(), encoding="utf-8")
    base = {"account_id": "acc", "zone_id": "zone", "app": "paisa", "hostnames": ["app.example.com"]}
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({**base, **values}), encoding="utf-8")
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(tmp_path), "TF_IN_AUTOMATION": "1"}
    subprocess.run(["tofu", "init", "-input=false", "-no-color"], cwd=tmp_path, env=env, check=True, capture_output=True)
    done = subprocess.run(["tofu", "console", "-no-color"], cwd=tmp_path, env=env, check=True, capture_output=True,
                          text=True, input="jsonencode(local.ingress)\n")
    return json.loads(json.loads(done.stdout.strip()))


GATED = {"gated": True, "team_name": "team", "aud_by_hostname": {"app.example.com": "aud-1"}}


def test_a_gated_app_with_webhook_paths_gets_the_path_rule_first_and_the_access_rule_second(tmp_path):
    rules = ingress(tmp_path, **GATED, webhook_paths=["/api/webhooks/clerk", "/api/ingest/discord"])
    assert [rule["hostname"] for rule in rules] == ["app.example.com", "app.example.com", None]
    bypass, gated, catch_all = rules
    assert bypass == {"hostname": "app.example.com", "path": r"^(/api/webhooks/clerk|/api/ingest/discord)(/.*)?$",
                      "service": TRAEFIK, "origin_request": None}
    assert gated["path"] is None and gated["service"] == TRAEFIK
    assert gated["origin_request"] == {"access": {"required": True, "team_name": "team", "aud_tag": ["aud-1"]}}
    assert catch_all == {"hostname": None, "path": None, "service": "http_status:404", "origin_request": None}


def test_the_path_regex_matches_the_declared_paths_and_what_is_under_them_only(tmp_path):
    (path,) = [rule["path"] for rule in ingress(tmp_path, **GATED, webhook_paths=["/api/webhooks/clerk/"]) if rule["path"]]
    pattern = re.compile(path)  # cloudflared uses Go's RE2; this pattern means the same in Python
    for hit in ("/api/webhooks/clerk", "/api/webhooks/clerk/", "/api/webhooks/clerk/user.created"):
        assert pattern.search(hit), hit
    for miss in ("/", "/api/webhooks", "/api/webhooks/clerkx", "/x/api/webhooks/clerk", "/dashboard"):
        assert not pattern.search(miss), miss


def test_regex_characters_in_a_declared_path_are_escaped(tmp_path):
    (path,) = [rule["path"] for rule in ingress(tmp_path, **GATED, webhook_paths=["/hooks/v1.0+(x)"]) if rule["path"]]
    assert path == r"^(/hooks/v1\.0\+\(x\))(/.*)?$"
    assert re.search(path, "/hooks/v1.0+(x)") and not re.search(path, "/hooks/v1x0+x")


def test_without_webhook_paths_or_without_a_gate_the_ingress_is_unchanged(tmp_path):
    plain = ingress(tmp_path / "a", **GATED)
    assert [rule["path"] for rule in plain] == [None, None] and plain[0]["origin_request"]["access"]["required"] is True
    public = ingress(tmp_path / "b", webhook_paths=["/api/webhooks/clerk"])
    assert [(rule["hostname"], rule["origin_request"]) for rule in public] == [("app.example.com", None), (None, None)]


def test_every_gated_hostname_gets_its_own_path_rule_before_any_access_rule(tmp_path):
    hosts = ["a.example.com", "b.example.com"]
    rules = ingress(tmp_path, hostnames=hosts, gated=True, team_name="team",
                    aud_by_hostname={host: f"aud-{host}" for host in hosts}, webhook_paths=["/hook"])
    assert [(rule["hostname"], bool(rule["path"]), rule["origin_request"] is not None) for rule in rules] == [
        ("a.example.com", True, False), ("b.example.com", True, False),
        ("a.example.com", False, True), ("b.example.com", False, True), (None, False, False)]


def test_the_edge_stack_passes_the_manifest_webhook_paths_to_the_tunnel():
    stack = (REPO / "tofu" / "stacks" / "edge" / "main.tf").read_text(encoding="utf-8")
    module = stack.split('module "ingress" {', 1)[1].split("\n}\n", 1)[0]
    assert "webhook_paths   = try(each.value.manifest.user_auth.webhook_paths, [])" in module
