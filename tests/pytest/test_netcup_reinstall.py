"""D-070: `reinstall` wipes and reinstalls a Netcup server through the SCP REST API."""
from __future__ import annotations

import base64
import io
import json
import re

import pytest
import yaml

from sealedstack import cli, commands, config as config_mod, infraci, reinstall, tailnet
from sealedstack.http import HttpClient, Response
from test_engine_server import REPO, FakeSource, make_config

BASE = reinstall.API
CREDS = {
    "NETCUP_REFRESH_TOKEN": "refresh-1",
    "ROOT_PASSWORD_WEB_1": "Correct-Horse-9-Battery",
    "SERVER_IP_WEB_1": "203.0.113.10",
    "TAILSCALE_OAUTH_CLIENT_ID": "id",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
}
FLAVOURS = [
    {"id": 11, "name": "Ubuntu 22.04", "alias": "ubuntu-22.04", "text": "x"},
    {"id": 12, "name": "Ubuntu 24.04", "alias": "ubuntu-24.04", "text": "x"},
    {"id": 13, "name": "Debian 12", "alias": "debian-12", "text": "x"},
]
SERVER = {"id": 4711, "name": "v2202012345678", "hostname": "web-1", "serverLiveInfo": {"disks": [{"dev": "vda"}]}}


class FakeNetcup(HttpClient):
    def __init__(self, *, servers=None, flavours=None, states=("RUNNING", "FINISHED"), token_status=200, start_status=202):
        self.servers = [SERVER] if servers is None else servers
        self.flavours = FLAVOURS if flavours is None else flavours
        self.states = list(states)
        self.token_status, self.start_status = token_status, start_status
        self.calls = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url, dict(headers or {}), json_body, form_body))
        if url == reinstall.TOKEN_URL:
            return Response(self.token_status, {}, json.dumps({"access_token": "access-1"}).encode(), url)
        assert headers["Authorization"] == "Bearer access-1"
        path = url[len(BASE):]
        if method == "GET" and path == "/servers":
            return Response(200, {}, json.dumps([{"id": s["id"], "name": s["name"]} for s in self.servers]).encode(), url)
        if method == "GET" and re.fullmatch(r"/servers/\d+", path):
            found = [s for s in self.servers if path.endswith(f"/{s['id']}")]
            return Response(200 if found else 404, {}, json.dumps(found[0] if found else {}).encode(), url)
        if method == "GET" and path.endswith("/imageflavours"):
            return Response(200, {}, json.dumps(self.flavours).encode(), url)
        if method == "POST" and path.endswith("/image"):
            return Response(self.start_status, {}, json.dumps({"uuid": "task-1", "state": "PENDING"}).encode(), url)
        if method == "GET" and path == "/tasks/task-1":
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            body = {"uuid": "task-1", "state": state, "message": "disk full" if state == "ERROR" else None}
            return Response(200, {}, json.dumps(body).encode(), url)
        raise AssertionError(f"unexpected call {method} {url}")

    def posted(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/image")]


class Env:
    """The collaborators `run` is given, recording what it did."""

    def __init__(self, tmp_path, *, data=None, spec=None, probe=True, removed=1):
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.config = make_config(tmp_path)
        servers = yaml.safe_load((self.config.config_dir / "servers.yaml").read_text())
        servers["servers"]["web-1"].update({"external_id": "v2202012345678", **(spec or {})})
        (self.config.config_dir / "servers.yaml").write_text(yaml.safe_dump(servers))
        self.config = config_mod.load_config(config_dir=self.config.config_dir, code_root=REPO, env={})
        self.source = FakeSource(CREDS if data is None else data)
        self.http = FakeNetcup()
        self.cleared, self.removed_nodes, self.probes, self.out = [], [], [], io.StringIO()
        self._probe, self._removed = probe, removed

    def go(self, *args, http=None):
        return reinstall.run(
            self.config, {}, list(args),
            source=self.source, http=http or self.http, out=self.out, sleep=lambda s: None,
            forget_progress=lambda: self.cleared.append(True),
            remove_node=lambda name, cid, sec: (self.removed_nodes.append(name), self._removed)[1],
            tcp_probe=lambda host, port, timeout: (self.probes.append((host, port)), self._probe)[1],
        )

    @property
    def text(self):
        return self.out.getvalue()


def test_a_dry_run_shows_the_plan_and_changes_nothing(tmp_path):
    env = Env(tmp_path)
    assert env.go("--name", "web-1", "--dry-run") == 0
    assert env.http.posted() == []
    assert "Ubuntu 24.04" in env.text and "vda" in env.text and "web-1" in env.text
    assert env.cleared == [] and env.removed_nodes == []


def test_without_the_typed_name_it_refuses_and_wipes_nothing(tmp_path):
    env = Env(tmp_path)
    assert env.go("--name", "web-1") == 1
    assert env.http.posted() == []
    assert "--confirm web-1" in env.text
    assert env.go("--name", "web-1", "--confirm", "web-2") == 1 and env.http.posted() == []


def test_the_install_request_carries_image_disk_hostname_and_a_root_password_script(tmp_path):
    env = Env(tmp_path)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 0
    (_, url, _, body, _), = env.http.posted()
    assert url == f"{BASE}/servers/4711/image"
    assert body["imageFlavourId"] == 12 and body["diskName"] == "vda" and body["rootPartitionFullDiskSize"] is True
    assert body["hostname"] == "web-1" and body["sshPasswordAuthentication"] is True
    script = body["customScript"]
    encoded = re.search(r"echo '([A-Za-z0-9+/=]+)' \| base64 -d \| chpasswd", script).group(1)
    assert base64.b64decode(encoded).decode() == "root:Correct-Horse-9-Battery"
    assert "PermitRootLogin yes" in script and "Correct-Horse" not in script


def test_the_refresh_token_is_exchanged_for_an_access_token(tmp_path):
    env = Env(tmp_path)
    env.go("--name", "web-1", "--confirm", "web-1")
    method, url, _, _, form = env.http.calls[0]
    assert (method, url) == ("POST", reinstall.TOKEN_URL)
    assert form == {"grant_type": "refresh_token", "client_id": "scp", "refresh_token": "refresh-1"}


def test_after_success_the_old_tailnet_node_and_progress_are_forgotten_and_next_steps_printed(tmp_path):
    env = Env(tmp_path)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 0
    assert env.removed_nodes == ["web-1"] and env.cleared == [True]
    assert env.probes and env.probes[0] == ("203.0.113.10", 22)
    assert "sealedstack up" in env.text


def test_a_failed_task_leaves_the_tailnet_node_and_progress_alone(tmp_path):
    env = Env(tmp_path)
    env.http = FakeNetcup(states=("RUNNING", "ERROR"))
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "disk full" in env.text
    assert env.removed_nodes == [] and env.cleared == []


def test_a_task_that_never_finishes_times_out(tmp_path, monkeypatch):
    env = Env(tmp_path)
    env.http = FakeNetcup(states=("RUNNING",))
    monkeypatch.setattr(reinstall, "TASK_TIMEOUT", 30)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "did not finish" in env.text


def test_secrets_never_appear_in_output(tmp_path):
    env = Env(tmp_path)
    env.go("--name", "web-1", "--confirm", "web-1")
    for secret in ("refresh-1", "access-1", "Correct-Horse", "203.0.113.10"):
        assert secret not in env.text


@pytest.mark.parametrize("missing", ["NETCUP_REFRESH_TOKEN", "ROOT_PASSWORD_WEB_1"])
def test_a_missing_secret_names_the_key_and_stops_before_any_call(tmp_path, missing):
    env = Env(tmp_path, data={k: v for k, v in CREDS.items() if k != missing})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert missing in env.text and env.http.calls == []


@pytest.mark.parametrize("password", ["short", "has space in it-123", "new\nline-password-1"])
def test_a_weak_or_unsafe_root_password_is_refused(tmp_path, password):
    env = Env(tmp_path, data={**CREDS, "ROOT_PASSWORD_WEB_1": password})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "root password" in env.text and env.http.posted() == []


def test_only_netcup_servers_can_be_reinstalled(tmp_path):
    env = Env(tmp_path, spec={"provider": "hetzner"})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "netcup" in env.text.lower() and env.http.calls == []


def test_external_id_is_required_and_must_exist_in_the_account(tmp_path):
    env = Env(tmp_path, spec={"external_id": ""})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1 and "external_id" in env.text
    env = Env(tmp_path / "b", spec={"external_id": "v22nope"})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1 and "v22nope" in env.text
    assert env.http.posted() == []


def test_a_numeric_external_id_matches_the_server_id(tmp_path):
    env = Env(tmp_path, spec={"external_id": "4711"})
    assert env.go("--name", "web-1", "--dry-run") == 0


def test_an_unknown_or_ambiguous_image_lists_the_choices(tmp_path):
    env = Env(tmp_path, spec={"os": "rocky-9"})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "Ubuntu 24.04" in env.text and env.http.posted() == []
    twin = FLAVOURS + [{"id": 14, "name": "Debian 12 minimal", "alias": "debian-12-min", "text": "x"}]
    env = Env(tmp_path / "b", spec={"os": "debian-12"})
    env.http = FakeNetcup(flavours=[f for f in twin if f["id"] != 13] + [{"id": 15, "name": "Debian 12 desktop", "alias": "debian-12-desktop", "text": "x"}])
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1 and "--flavour" in env.text


def test_the_flavour_flag_overrides_the_choice(tmp_path):
    env = Env(tmp_path)
    assert env.go("--name", "web-1", "--confirm", "web-1", "--flavour", "13") == 0
    assert env.http.posted()[0][3]["imageFlavourId"] == 13


def test_a_server_with_several_disks_needs_an_explicit_one(tmp_path):
    env = Env(tmp_path)
    env.http = FakeNetcup(servers=[{**SERVER, "serverLiveInfo": {"disks": [{"dev": "vda"}, {"dev": "vdb"}]}}])
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1 and "--disk" in env.text
    assert env.go("--name", "web-1", "--confirm", "web-1", "--disk", "vdb") == 0
    assert env.http.posted()[0][3]["diskName"] == "vdb"


def test_a_rejected_login_says_how_to_renew_the_token(tmp_path):
    env = Env(tmp_path)
    env.http = FakeNetcup(token_status=400)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 1
    assert "refresh token" in env.text and "netcup-reinstall" in env.text


def test_a_pinned_host_key_is_flagged_as_outdated(tmp_path):
    fingerprint = "SHA256:" + "A" * 43
    env = Env(tmp_path, spec={"bootstrap": {"host_key_fingerprint": fingerprint}})
    assert env.go("--name", "web-1", "--confirm", "web-1") == 0
    assert "host_key_fingerprint" in env.text and "new" in env.text.lower()


def test_ssh_not_answering_is_a_warning_not_a_failure(tmp_path):
    env = Env(tmp_path, probe=False)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 0
    assert "port 22" in env.text


# -- the tailnet side ----------------------------------------------------------------------------
class FakeTailscale(HttpClient):
    def __init__(self, devices, *, delete_status=200):
        self.devices, self.delete_status, self.calls = devices, delete_status, []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url))
        if url.endswith("/oauth/token"):
            return Response(200, {}, json.dumps({"access_token": "tok"}).encode(), url)
        if method == "DELETE":
            return Response(self.delete_status, {}, b"", url)
        return Response(200, {}, json.dumps({"devices": self.devices}).encode(), url)


def test_remove_node_deletes_only_the_tagged_node_of_that_name():
    http = FakeTailscale([
        {"id": "1", "name": "web-1.t.ts.net", "hostname": "web-1", "tags": ["tag:server"]},
        {"id": "2", "name": "web-10.t.ts.net", "hostname": "web-10", "tags": ["tag:server"]},
        {"id": "3", "name": "web-1.t.ts.net", "hostname": "web-1", "tags": []},
    ])
    assert tailnet.remove_node("web-1", "id", "sec", http=http) == 1
    assert [c for c in http.calls if c[0] == "DELETE"] == [("DELETE", f"{tailnet.API}/device/1")]


def test_remove_node_reports_unknown_when_the_tailnet_refuses():
    assert tailnet.remove_node("web-1", "id", "sec", http=FakeTailscale([{"id": "1", "hostname": "web-1", "name": "web-1.t", "tags": ["tag:server"]}], delete_status=403)) is None


# -- wiring --------------------------------------------------------------------------------------
def test_reinstall_is_a_guarded_write_command_in_both_spellings():
    from sealedstack import approval

    assert "server reinstall" in config_mod.WRITE_COMMANDS and approval.requires_approval("reinstall")


def test_it_can_be_dispatched_but_not_run_from_a_comment():
    assert "reinstall" in infraci.DISPATCH_COMMANDS and "reinstall" in commands.COMMANDS
    assert "reinstall" not in infraci.COMMENT_READ + infraci.COMMENT_WRITE
    assert "reinstall" in infraci.NO_MESH


def test_the_cli_parses_both_spellings_and_passes_flags_through():
    parser = cli.build_parser()
    assert cli.canonical(parser.parse_args(["reinstall"])) == "server reinstall"
    assert cli.canonical(parser.parse_args(["server", "reinstall"])) == "server reinstall"
    head, tail = cli.split_passthrough(["reinstall", "--name", "web-1", "--confirm", "web-1"])
    assert head == ["reinstall"] and tail == ["--name", "web-1", "--confirm", "web-1"]
    head, tail = cli.split_passthrough(["server", "reinstall", "--name", "web-1"])
    assert head == ["server", "reinstall"] and tail == ["--name", "web-1"]


def test_a_local_session_refuses_without_break_glass(tmp_path, capsys):
    rc = cli.run(["reinstall", "--name", "web-1", "--confirm", "web-1"], code_root=REPO, env={"SEALEDSTACK_CONFIG": str(make_config(tmp_path).config_dir)})
    assert rc == 2 and "break-glass" in capsys.readouterr().err.lower()


# -- Netcup's real shape: the flavour is a variant, the OS is on `image` (live run 2026-11-01) ------------------
REAL = [
    {"id": 123, "name": "Minimal", "alias": "Minimal", "text": "x", "image": {"id": 1, "name": "Debian 12"}},
    {"id": 52, "name": "Standard", "alias": "Standard", "text": "x", "image": {"id": 2, "name": "Ubuntu 24.04"}},
    {"id": 22, "name": "Minimal", "alias": "Minimal", "text": "x", "image": {"id": 2, "name": "Ubuntu 24.04"}},
    {"id": 126, "name": "Minimal", "alias": "Minimal", "text": "x", "image": {"id": 3, "name": "Ubuntu 22.04"}},
]


def test_the_os_is_matched_on_the_image_name_and_the_minimal_variant_wins(tmp_path):
    env = Env(tmp_path)
    env.http = FakeNetcup(flavours=REAL)
    assert env.go("--name", "web-1", "--confirm", "web-1") == 0
    assert env.http.posted()[0][3]["imageFlavourId"] == 22


def test_the_dry_run_names_the_os_not_just_the_variant(tmp_path):
    env = Env(tmp_path)
    env.http = FakeNetcup(flavours=REAL)
    assert env.go("--name", "web-1", "--dry-run") == 0
    assert "Ubuntu 24.04, Minimal (id 22)" in env.text


def test_an_unknown_os_lists_images_with_their_os_names(tmp_path):
    env = Env(tmp_path, spec={"os": "rocky-9"})
    env.http = FakeNetcup(flavours=REAL)
    assert env.go("--name", "web-1", "--dry-run") == 1
    assert "Debian 12, Minimal (id 123)" in env.text and "Ubuntu 22.04" in env.text


def test_several_variants_without_a_minimal_one_ask_for_a_choice(tmp_path):
    two = [f for f in REAL if f["id"] != 22] + [{"id": 53, "name": "Docker", "alias": "Docker", "text": "x", "image": {"id": 2, "name": "Ubuntu 24.04"}}]
    env = Env(tmp_path)
    env.http = FakeNetcup(flavours=two)
    assert env.go("--name", "web-1", "--dry-run") == 1 and "--flavour" in env.text
