"""D-074: the Netcup provider firewall is managed through the SCP API (live run 2026-11-01: UDP 7844 replies were never allowed)."""
from __future__ import annotations

import base64
import io
import json
import re

import pytest
import yaml

from sealedstack import config as config_mod, netcupfw, reinstall
from sealedstack.http import HttpClient, Response
from sealedstack.secrets import SecretResult
from test_engine_server import REPO, FakeSource, make_config

BASE = reinstall.API
MAC = "nic0"  # opaque to the code; a MAC-shaped literal trips the publication hygiene check
SERVER = {"id": 4711, "name": "v2202012345678", "architecture": "AMD64",
          "serverLiveInfo": {"disks": [{"dev": "vda"}], "interfaces": [{"mac": MAC, "ipv4Addresses": ["198.51.100.7"]}]}}


def jwt(claims: dict) -> str:
    def part(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


class FakeScp(HttpClient):
    def __init__(self, *, policies=None, attached=None, active=True, details=None):
        self.policies = policies if policies is not None else [{"id": 9, "name": "g"}]
        self.details = details or {}
        self.attached = attached if attached is not None else [{"id": 9, "name": "g"}]
        self.active = active
        self.calls = []
        self.created = None

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url.replace(BASE, ""), json_body))
        if url == reinstall.TOKEN_URL:
            return Response(200, {}, json.dumps({"access_token": jwt({"id": "77"})}).encode(), url)
        path = url[len(BASE):]
        ok = lambda body, status=200: Response(status, {}, json.dumps(body).encode(), url)  # noqa: E731
        if method == "GET" and path == "/servers":
            return ok([{"id": 4711, "name": "v2202012345678"}])
        if method == "GET" and path == "/servers/4711":
            return ok(SERVER)
        if method == "GET" and path == f"/servers/4711/interfaces/{MAC}/firewall":
            return ok({"active": self.active, "copiedPolicies": [{"id": 5, "name": "netcup Mail block"}], "userPolicies": self.attached})
        if method == "GET" and path == "/users/77/firewall-policies":
            return ok(self.policies)
        m = re.fullmatch(r"/users/77/firewall-policies/(\d+)", path)
        if method == "GET" and m:
            return ok({"id": int(m.group(1)), "name": "sealedstack", "rules": self.details.get(int(m.group(1)), [])})
        if method == "POST" and path == "/users/77/firewall-policies":
            self.created = json_body
            return ok({"id": 42, **json_body}, 201)
        if method == "PUT" and m:
            return ok({"id": int(m.group(1))})
        if method == "PUT" and path == f"/servers/4711/interfaces/{MAC}/firewall":
            return ok({"uuid": "task-1", "state": "PENDING"}, 202)
        if method == "GET" and path == "/tasks/task-1":
            return ok({"uuid": "task-1", "state": "FINISHED"})
        raise AssertionError(f"unexpected {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] in ("POST", "PUT") and c[1] != "" and "openid" not in c[1]]


def cfg(tmp_path, **spec):
    tmp_path.mkdir(parents=True, exist_ok=True)
    config = make_config(tmp_path)
    servers = yaml.safe_load((config.config_dir / "servers.yaml").read_text())
    servers["servers"]["web-1"].update({"external_id": "v2202012345678", **spec})
    (config.config_dir / "servers.yaml").write_text(yaml.safe_dump(servers))
    return config_mod.load_config(config_dir=config.config_dir, code_root=REPO, env={})


def go(tmp_path, http, *, ssh="closed", dry_run=False, **spec):
    out = io.StringIO()
    changed = netcupfw.apply(cfg(tmp_path, **spec), {}, "web-1", ssh=ssh, dry_run=dry_run,
                             source=FakeSource({"NETCUP_REFRESH_TOKEN": "refresh"}), http=http, out=out, sleep=lambda s: None)
    return changed, out.getvalue()


def test_the_rule_table_allows_cloudflared_replies_and_drops_the_rest_and_never_opens_web_ports():
    rules = netcupfw.desired_rules(ssh_open=False)
    sigs = [netcupfw._signature(r) for r in rules]
    assert ("INGRESS", "UDP", "ACCEPT", "7844", "") in sigs  # the rule the first live run was missing
    assert ("INGRESS", "UDP", "ACCEPT", "", "41641") in sigs
    assert all(s[4] not in ("22", "80", "443") for s in sigs)  # nothing opens 22 (closed), 80 or 443
    assert sigs[-2:] == [("INGRESS", "TCP", "DROP", "", ""), ("INGRESS", "UDP", "DROP", "", "")]  # drops come last


def test_open_adds_exactly_one_rule_before_the_drops():
    closed, opened = netcupfw.desired_rules(ssh_open=False), netcupfw.desired_rules(ssh_open=True)
    assert len(opened) == len(closed) + 1
    extra = [r for r in opened if r not in closed][0]
    assert (extra["protocol"], extra["action"], extra["destinationPorts"]) == ("TCP", "ACCEPT", "22")
    assert opened.index(extra) < opened.index(opened[-2])


def test_a_dry_run_reports_and_writes_nothing(tmp_path):
    http = FakeScp()
    changed, text = go(tmp_path, http, dry_run=True)
    assert changed is True and http.writes() == [] and "will be created" in text and "g (not deleted)" in text


def test_it_creates_the_policy_attaches_only_it_and_keeps_netcups_copied_policies(tmp_path):
    http = FakeScp()
    changed, text = go(tmp_path, http)
    assert changed and http.created["name"] == "sealedstack" and http.created["rules"] == netcupfw.desired_rules(ssh_open=False)
    put = [c for c in http.calls if c[0] == "PUT" and c[1].endswith("/firewall")][0]
    assert put[2] == {"copiedPolicies": [{"id": 5}], "userPolicies": [{"id": 42}], "active": True}
    assert "applied" in text and "refresh" not in text


def test_an_up_to_date_setup_changes_nothing(tmp_path):
    wanted = netcupfw.desired_rules(ssh_open=False)
    http = FakeScp(policies=[{"id": 42, "name": "sealedstack"}], attached=[{"id": 42, "name": "sealedstack"}], details={42: wanted})
    changed, text = go(tmp_path, http)
    assert changed is False and http.writes() == [] and "nothing to change" in text


def test_a_policy_with_other_rules_is_updated_in_place(tmp_path):
    http = FakeScp(policies=[{"id": 42, "name": "sealedstack"}], attached=[{"id": 42, "name": "sealedstack"}], details={42: netcupfw.desired_rules(ssh_open=True)})
    changed, _ = go(tmp_path, http, ssh="closed")
    assert changed
    update = [c for c in http.calls if c[0] == "PUT" and c[1] == "/users/77/firewall-policies/42"][0]
    assert update[2]["rules"] == netcupfw.desired_rules(ssh_open=False)


def test_an_inactive_firewall_is_activated(tmp_path):
    wanted = netcupfw.desired_rules(ssh_open=False)
    http = FakeScp(policies=[{"id": 42, "name": "sealedstack"}], attached=[{"id": 42, "name": "sealedstack"}], details={42: wanted}, active=False)
    changed, text = go(tmp_path, http)
    assert changed and "inactive" in text


def test_only_netcup_servers_with_an_external_id_are_handled(tmp_path):
    with pytest.raises(netcupfw.FirewallError, match="external_id"):
        netcupfw.apply(cfg(tmp_path, external_id=""), {}, "web-1", source=FakeSource({"NETCUP_REFRESH_TOKEN": "x"}), http=FakeScp(), out=io.StringIO())
    with pytest.raises(netcupfw.FirewallError, match="not a Netcup"):
        netcupfw.apply(cfg(tmp_path / "b", provider="hetzner"), {}, "web-1", source=FakeSource({"NETCUP_REFRESH_TOKEN": "x"}), http=FakeScp(), out=io.StringIO())


def test_the_user_id_comes_from_the_token_and_a_token_without_it_is_refused():
    assert netcupfw.user_id(jwt({"id": "123"})) == "123"
    with pytest.raises(netcupfw.FirewallError):
        netcupfw.user_id(jwt({"sub": "x"}))
    with pytest.raises(netcupfw.FirewallError):
        netcupfw.user_id("not-a-jwt")


# -- wiring into `up` -----------------------------------------------------------------------------------------------
from test_engine_server import FakeIO, fake_generated  # noqa: E402
from sealedstack import server  # noqa: E402

UP_SECRETS = {"SERVER_IP_WEB_1": "203.0.113.10", "ROOT_PASSWORD_WEB_1": "rootpw",
              "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}


def test_up_opens_22_before_the_first_login_and_closes_it_after_the_baseline_without_a_prompt(tmp_path):
    config = cfg(tmp_path)
    io, calls = FakeIO(captures={"sealedstack.tailscalekey": "tskey"}), []
    original_run = io.run

    def run(argv, env):
        calls.append("ansible:" + ("bootstrap" if "bootstrap.yml" in " ".join(argv) else "site"))
        return original_run(argv, env)

    io.run = run
    server.up(config, {}, "web-1", source=FakeSource(UP_SECRETS), io=io, generated=fake_generated(),
              firewall=lambda ssh, out, **kw: calls.append("firewall:" + ssh) or True)
    assert calls == ["firewall:open", "ansible:bootstrap", "ansible:site", "firewall:closed"]
    assert io.prompts == []  # nothing for the operator to do by hand


def test_without_the_netcup_token_up_keeps_the_manual_steps(tmp_path):
    config = cfg(tmp_path)
    io = FakeIO(captures={"sealedstack.tailscalekey": "tskey"})
    server.up(config, {}, "web-1", source=FakeSource(UP_SECRETS), io=io, generated=fake_generated())
    assert io.prompts  # the rule table is printed and the operator confirms


def test_managed_firewall_needs_netcup_an_external_id_and_the_refresh_token(tmp_path):
    config = cfg(tmp_path)
    with_token = FakeSource({"NETCUP_REFRESH_TOKEN": "x"})
    assert server.managed_firewall(config, with_token, "netcup", "web-1") is not None
    assert server.managed_firewall(config, FakeSource({}), "netcup", "web-1") is None
    assert server.managed_firewall(config, with_token, "hetzner", "web-1") is None
    assert server.managed_firewall(cfg(tmp_path / "b", external_id=""), with_token, "netcup", "web-1") is None
