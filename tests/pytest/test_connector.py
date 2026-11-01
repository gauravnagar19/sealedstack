"""D-073: the engine keeps the tunnel connector fed with its token (live run 2026-11-01, Cloudflare error 1033)."""
from __future__ import annotations

import io
import json

from sealedstack import connector, orchestrate
from sealedstack.http import HttpClient, Response
from sealedstack.secrets import EnvSource

from test_engine_server import REPO, make_config

TOKEN = "eyJ-tunnel-token"
OUTPUTS = {"connector_tokens": {"hello": TOKEN}, "tunnel_ids": {"hello": "tun-1"}, "ingress_type": "cloudflare"}


class FakeDokploy:
    """Just enough of DokployClient: composes, env, deploys."""

    def __init__(self, env="", composes=("c1",), name="hello-connector"):
        self.envs = {cid: env for cid in composes}
        self.name = name
        self.calls = []
        self._deploys = {cid: [] for cid in composes}

    def get(self, path):
        assert path == "/api/project.all"
        return [{"environments": [{"compose": [{"composeId": cid, "name": self.name} for cid in self.envs]}]}]

    def compose_env(self, cid):
        return self.envs[cid]

    def compose_update(self, cid, env):
        self.calls.append(("update", cid, env))
        self.envs[cid] = env

    def compose_deploy(self, cid):
        self.calls.append(("deploy", cid))
        self._deploys[cid].append({"deploymentId": f"d{len(self._deploys[cid]) + 1}", "status": "done", "createdAt": f"2026-11-01T00:00:0{len(self._deploys[cid])}"})

    def deployments(self, cid):
        return list(self._deploys[cid])


class FakeCloudflare(HttpClient):
    def __init__(self, connected):
        self.connected = list(connected)  # one answer per call; the last repeats
        self.calls = 0

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        assert url.endswith("/cfd_tunnel/tun-1") and headers["Authorization"] == "Bearer cf-token"
        answer = self.connected[min(self.calls, len(self.connected) - 1)]
        self.calls += 1
        connections = [{"id": "x"}] if answer else []
        return Response(200, {}, json.dumps({"result": {"status": "healthy" if answer else "inactive", "connections": connections}}).encode(), url)


def run(tmp_path, dokploy, cloudflare, outputs=None):
    out = io.StringIO()
    config = make_config(tmp_path)
    rc = connector.run(
        config, {}, source=EnvSource({"CLOUDFLARE_API_TOKEN": "cf-token"}), client=dokploy, http=cloudflare,
        sleep=lambda s: None, out=out, outputs=OUTPUTS if outputs is None else outputs,
    )
    return rc, out.getvalue()


def test_a_missing_token_is_set_and_the_connector_deployed_until_the_tunnel_connects(tmp_path):
    dok = FakeDokploy(env="")
    rc, text = run(tmp_path, dok, FakeCloudflare([False, False, True]))
    assert rc == 0 and "connected" in text
    assert dok.envs["c1"] == f"TUNNEL_TOKEN={TOKEN}"
    assert [c[0] for c in dok.calls] == ["update", "deploy"]


def test_other_env_lines_are_kept(tmp_path):
    dok = FakeDokploy(env="FOO=bar\nTUNNEL_TOKEN=old\nBAZ=1")
    run(tmp_path, dok, FakeCloudflare([False, True]))
    assert dok.envs["c1"] == f"FOO=bar\nTUNNEL_TOKEN={TOKEN}\nBAZ=1"


def test_a_connected_tunnel_is_never_restarted_even_when_the_env_had_to_be_restored(tmp_path):
    """The tofu provider clears env on every apply; restoring it must not bounce a healthy tunnel."""
    dok = FakeDokploy(env="")
    rc, text = run(tmp_path, dok, FakeCloudflare([True]))
    assert rc == 0 and "no restart needed" in text
    assert [c[0] for c in dok.calls] == ["update"]


def test_nothing_to_do_when_everything_is_in_place(tmp_path):
    dok = FakeDokploy(env=f"TUNNEL_TOKEN={TOKEN}")
    rc, text = run(tmp_path, dok, FakeCloudflare([True]))
    assert rc == 0 and dok.calls == []


def test_a_tunnel_that_never_connects_fails_with_the_firewall_hint(tmp_path):
    dok = FakeDokploy(env="")
    rc, text = run(tmp_path, dok, FakeCloudflare([False]))
    assert rc == 1 and "UDP source port 7844" in text and "netcup-firewall" in text
    assert [c[0] for c in dok.calls] == ["update", "deploy"]  # deployed once, not in a loop


def test_every_server_gets_the_token(tmp_path):
    dok = FakeDokploy(env="", composes=("c1", "c2"))
    run(tmp_path, dok, FakeCloudflare([False, True]))
    assert dok.envs["c1"] == dok.envs["c2"] == f"TUNNEL_TOKEN={TOKEN}"
    assert [c for c in dok.calls if c[0] == "deploy"] == [("deploy", "c1"), ("deploy", "c2")]


def test_no_connectors_is_not_an_error(tmp_path):
    rc, text = run(tmp_path, FakeDokploy(), FakeCloudflare([True]), outputs={"connector_tokens": {}})
    assert rc == 0 and "no tunnel connectors" in text


def test_an_app_without_a_connector_compose_yet_is_skipped(tmp_path):
    rc, text = run(tmp_path, FakeDokploy(name="other-connector"), FakeCloudflare([True]))
    assert rc == 0 and "no 'hello-connector' compose" in text


def test_ngrok_uses_its_own_variable_and_is_not_probed_through_cloudflare(tmp_path):
    dok = FakeDokploy(env="")
    rc, _ = run(tmp_path, dok, FakeCloudflare([True]), outputs={"connector_tokens": {"hello": "ng"}, "ingress_type": "ngrok"})
    assert rc == 0 and dok.envs["c1"] == "NGROK_AUTHTOKEN=ng"


def test_with_variable_handles_duplicates_and_missing_final_newline():
    text, changed = connector.with_variable("A=1\nTUNNEL_TOKEN=x\nTUNNEL_TOKEN=y", "TUNNEL_TOKEN", "z")
    assert text == "A=1\nTUNNEL_TOKEN=z" and changed
    assert connector.with_variable("TUNNEL_TOKEN=z", "TUNNEL_TOKEN", "z") == ("TUNNEL_TOKEN=z", False)


def test_the_connector_phase_runs_after_the_platform_apply_in_up_and_apply():
    for command in ("up", "apply"):
        names = [p.name for p in orchestrate.phases(command, code_root=REPO, stack_name="x", servers=["server-1"])]
        assert names.index("connector") == names.index("platform-apply") + 1
        assert names.index("connector") < names.index("post-apply")


def test_the_tofu_module_no_longer_writes_the_connector_env():
    """Two writers for one Dokploy field is what lost the token: the engine owns it."""
    text = (REPO / "tofu" / "modules" / "platform_dokploy" / "main.tf").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert 'dokploy_environment_variables" "connector"' not in code
