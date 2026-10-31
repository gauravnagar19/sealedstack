"""DR-10: `up` asks the tailnet whether a server is already bootstrapped."""
from __future__ import annotations

import json

import pytest

from sealedstack import config as config_mod, server, tailnet
from sealedstack.http import HttpClient, Response
from test_engine_server import FakeIO, FakeSource, fake_generated, make_config

CREDS = {"TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}
BOTH = {"SERVER_IP_WEB_1": "203.0.113.10", "ROOT_PASSWORD_WEB_1": "rootpw"}


class FakeTailscale(HttpClient):
    def __init__(self, devices=None, *, token_status=200, devices_status=200):
        self.devices, self.token_status, self.devices_status = devices or [], token_status, devices_status
        self.calls = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url, dict(headers or {}), form_body))
        if url.endswith("/oauth/token"):
            return Response(self.token_status, {}, json.dumps({"access_token": "tok"}).encode(), url)
        assert headers["Authorization"] == "Bearer tok"
        return Response(self.devices_status, {}, json.dumps({"devices": self.devices}).encode(), url)


NODE = {"name": "web-1.tail1234.ts.net", "hostname": "web-1", "tags": ["tag:server"]}


# -- the lookup -------------------------------------------------------------------------------
def test_a_tagged_node_with_the_name_is_registered():
    http = FakeTailscale([NODE])
    assert tailnet.node_registered("web-1", "id", "sec", http=http) is True
    token_call = http.calls[0]
    assert token_call[3] == {"grant_type": "client_credentials"} and token_call[2]["Authorization"].startswith("Basic ")
    assert http.calls[1][1].endswith("/tailnet/-/devices")


def test_the_fqdn_prefix_also_counts_and_other_names_do_not():
    assert tailnet.node_registered("web-1", "id", "sec", http=FakeTailscale([{"name": "web-1.t.ts.net", "hostname": "x", "tags": ["tag:server"]}])) is True
    assert tailnet.node_registered("web-1", "id", "sec", http=FakeTailscale([{"name": "web-10.t.ts.net", "hostname": "web-10", "tags": ["tag:server"]}])) is False
    assert tailnet.node_registered("web-1", "id", "sec", http=FakeTailscale([])) is False


def test_an_untagged_node_of_that_name_does_not_count():
    assert tailnet.node_registered("web-1", "id", "sec", http=FakeTailscale([{"name": "web-1.t.ts.net", "hostname": "web-1", "tags": []}])) is False
    assert tailnet.node_registered("web-1", "id", "sec", http=FakeTailscale([{"name": "web-1.t.ts.net", "hostname": "web-1", "tags": ["tag:ci"]}])) is False


@pytest.mark.parametrize("http", [FakeTailscale(token_status=401), FakeTailscale(devices_status=403), FakeTailscale(devices_status=500)])
def test_anything_that_goes_wrong_is_unknown_not_false(http):
    assert tailnet.node_registered("web-1", "id", "sec", http=http) is None


def test_a_network_error_is_unknown():
    class Down(HttpClient):
        def request(self, *a, **kw):
            raise OSError("no route")

    assert tailnet.node_registered("web-1", "id", "sec", http=Down()) is None


def test_missing_credentials_are_unknown_without_a_request():
    http = FakeTailscale([NODE])
    assert tailnet.node_registered("web-1", "", "sec", http=http) is None and http.calls == []


# -- up ------------------------------------------------------------------------------------------
def io_with(answer):
    io = FakeIO()
    io.tailnet_node = lambda name, cid, secret, net: answer
    return io


def up(tmp_path, source_data, answer, **kw):
    io = io_with(answer)
    out = []
    kw_out = type("O", (), {"write": lambda self, s: out.append(s), "flush": lambda self: None})()
    server.up(make_config(tmp_path), {}, "web-1", source=FakeSource(source_data), io=io, generated=fake_generated(), out=kw_out)
    return io, "".join(out)


def bootstraps(io):
    return any("bootstrap.yml" in " ".join(r) for r in io.runs)


def test_forgetting_both_secrets_on_a_fresh_server_is_a_clear_error_not_a_skipped_bootstrap(tmp_path):
    with pytest.raises(config_mod.ConfigError, match=r"'web-1' is not on the tailnet and its bootstrap secrets are missing: add SERVER_IP_WEB_1 and ROOT_PASSWORD_WEB_1"):
        up(tmp_path, dict(CREDS), False)


def test_a_node_on_the_tailnet_skips_the_public_bootstrap_even_if_the_secrets_are_still_there(tmp_path):
    io, log = up(tmp_path, {**CREDS, **BOTH}, True)
    assert not bootstraps(io) and any("site.yml" in " ".join(r) for r in io.runs)
    assert "already a node on the tailnet" in log


def test_a_node_on_the_tailnet_without_secrets_is_re_asserted_as_before(tmp_path):
    io, log = up(tmp_path, dict(CREDS), True)
    assert not bootstraps(io) and any("site.yml" in " ".join(r) for r in io.runs)


def test_not_on_the_tailnet_with_the_secrets_bootstraps(tmp_path):
    io, _ = up(tmp_path, {**CREDS, **BOTH, "TAILSCALE_OAUTH_CLIENT_ID": "id"}, False)
    assert bootstraps(io)


def test_unknown_falls_back_to_the_old_rule_in_both_directions(tmp_path):
    io, log = up(tmp_path, dict(CREDS), None)  # no secrets: assume bootstrapped
    assert not bootstraps(io) and "could not ask the tailnet" in log and "assuming it is already bootstrapped" in log
    (tmp_path / "b").mkdir()
    io, _ = up(tmp_path / "b", {**CREDS, **BOTH}, None)  # secrets present: bootstrap
    assert bootstraps(io)


def test_without_oauth_credentials_the_tailnet_is_not_asked(tmp_path):
    io = FakeIO()
    asked = []
    io.tailnet_node = lambda *a: asked.append(a)
    # the lookup uses the OAuth client the auth key needs; without it nothing is asked
    assert server._registered_on_tailnet(make_config(tmp_path), "web-1", FakeSource(dict(BOTH)), io, None) is None
    assert asked == []


def test_a_server_created_by_the_provider_is_not_bootstrapped_twice(tmp_path):
    io = io_with(True)
    io.capture = lambda argv, env: (0, "203.0.113.50" if "sealedstack.compute" in " ".join(argv) else "tskey-auth-x")
    server.up(make_config(tmp_path, provider="hetzner"), {}, "web-1", source=FakeSource(dict(CREDS)), io=io, generated=fake_generated())
    assert not bootstraps(io) and any("site.yml" in " ".join(r) for r in io.runs)
