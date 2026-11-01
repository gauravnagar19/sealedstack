"""`verify` looks at the running system after `up` and `apply` (live run 2026-11-01: a human did this by hand, slowly)."""
from __future__ import annotations

import io
import threading
import time

import pytest

from sealedstack import orchestrate, verify
from sealedstack.secrets import EnvSource

from test_engine_server import REPO, make_config
from test_netcupfw import cfg

ACCESS = "https://team.cloudflareaccess.com/cdn-cgi/access/login/x"


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(verify, "EDGE_RETRY_AFTER", 0)


def config(tmp_path, access="owner-only", blocked=("/.env", "*.php")):
    config = cfg(tmp_path)
    config.apps["apps"] = {"web": {"source": {"image": "x"}, "servers": ["web-1"], "hostnames": ["web.example.com"], "access": access}}
    config.stack["edge_security"] = {"blocked_paths": list(blocked)}
    return config


def good_fetch(url, timeout=0):
    return (403, "") if url.endswith("/.env") else (302, ACCESS)


class Client:
    def __init__(self, status="done"):
        self.status = status

    def get(self, path):
        return [{"environments": [{"compose": [{"name": "web", "composeStatus": self.status}, {"name": "web-connector", "composeStatus": "error"}]}]}]


def go(tmp_path, *, fetcher=good_fetch, connector=lambda host, port, timeout=0: False, client=None, source=None, access="owner-only"):
    out = io.StringIO()
    rc = verify.run(config(tmp_path, access=access), {}, source=source or EnvSource({"SERVER_IP_WEB_1": "203.0.113.5"}),
                    client=client or Client(), fetcher=fetcher, connector=connector, out=out)
    return rc, out.getvalue()


def test_a_healthy_system_passes_every_probe(tmp_path):
    rc, text = go(tmp_path)
    assert rc == 0 and "❌" not in text
    assert "web.example.com redirects to the Cloudflare Access login" in text and "(403)" in text and "TCP 5432 closed" in text
    assert "every app's last deploy is done" in text  # the connector compose (named web-connector) is not an app


def test_a_gated_app_that_answers_without_access_fails(tmp_path):
    rc, text = go(tmp_path, fetcher=lambda url, timeout=0: (200, ""))
    assert rc == 1 and "should redirects to the Cloudflare Access login, got 200" in text


def test_a_public_app_only_has_to_answer(tmp_path):
    rc, text = go(tmp_path, access="public", fetcher=lambda url, timeout=0: (403, "") if url.endswith("/.env") else (200, ""))
    assert rc == 0 and "web.example.com answers" in text


def test_a_blocked_path_that_is_served_fails(tmp_path):
    rc, text = go(tmp_path, fetcher=lambda url, timeout=0: (200, ""))
    assert rc == 1 and "should be refused with 403" in text


def test_an_open_public_port_fails(tmp_path):
    rc, text = go(tmp_path, connector=lambda host, port, timeout=0: port == 22)
    assert rc == 1 and "public TCP 22 is OPEN" in text and "TCP 80 closed" in text


def test_a_missing_ip_secret_skips_the_port_probes_instead_of_failing(tmp_path):
    rc, text = go(tmp_path, source=EnvSource({}))
    assert rc == 0 and "SERVER_IP_WEB_1 is not in the secret source" in text


def test_an_app_that_did_not_finish_its_deploy_fails(tmp_path):
    rc, text = go(tmp_path, client=Client("error"))
    assert rc == 1 and "web: error" in text


def test_the_probes_run_at_the_same_time(tmp_path):
    """Seven connects of 0.3 s each must not add up: that was why checking by hand was slow."""
    seen = []

    def slow(host, port, timeout=0):
        seen.append(threading.get_ident())
        time.sleep(0.3)
        return False

    start = time.monotonic()
    rc, _ = go(tmp_path, connector=slow)
    assert rc == 0 and time.monotonic() - start < 1.0 and len(set(seen)) > 1


def test_verify_closes_up_and_apply_after_the_last_phase():
    for command in ("up", "apply"):
        names = [p.name for p in orchestrate.phases(command, code_root=REPO, stack_name="x", servers=["server-1"])]
        assert names[-1] == "verify"


def test_a_failed_edge_answer_is_asked_again_once_and_a_good_one_is_not(tmp_path):
    calls = []

    def flaky(url, timeout=0):  # the hostname answers on the second try only
        calls.append(url)
        if url.endswith("/.env"):
            return 403, ""
        return (302, ACCESS) if calls.count(url) > 1 else (0, "timed out")

    rc, text = go(tmp_path, fetcher=flaky)
    assert rc == 0 and calls.count("https://web.example.com/") == 2

    calls.clear()
    go(tmp_path / "again", fetcher=lambda url, timeout=0: calls.append(url) or good_fetch(url))
    assert calls.count("https://web.example.com/") == 1


def test_the_timeouts_are_short():
    assert verify.HTTP_TIMEOUT <= 5 and verify.CONNECT_TIMEOUT <= 2 and verify.DEPLOY_TIMEOUT <= 10


def test_verify_is_a_cli_command_and_a_comment_command():
    from sealedstack import cli, commands, infraci

    assert "verify" in commands.COMMANDS and "verify" in infraci.COMMENT_READ
    assert cli.build_parser().parse_args(["verify"]).noun == "verify"
