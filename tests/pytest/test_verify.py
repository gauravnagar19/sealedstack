"""`verify` looks at the running system after `up` and `apply` (live run 2026-11-01: a human did this by hand, slowly)."""
from __future__ import annotations

import io
import json
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


def webhook_go(tmp_path, poster, *, access="owner-only", paths=("/api/webhooks/clerk", "/api/ingest/discord")):
    """`verify` for an app whose rendered manifest declares `user_auth.webhook_paths` (D-090)."""
    conf = config(tmp_path, access=access)
    conf.code_root = tmp_path
    (tmp_path / ".build" / "apps").mkdir(parents=True)
    (tmp_path / ".build" / "apps" / "web.json").write_text(
        json.dumps({"hostnames": ["web.example.com"], "manifest": {"user_auth": {"webhook_paths": list(paths)}}}))
    posted = []

    def recording(url, timeout=0):
        posted.append(url)
        return poster(url)

    out = io.StringIO()
    fetcher = good_fetch if access != "public" else (lambda url, timeout=0: (403, "") if url.endswith("/.env") else (200, ""))
    rc = verify.run(conf, {}, source=EnvSource({"SERVER_IP_WEB_1": "203.0.113.5"}), client=Client(), fetcher=fetcher,
                    poster=recording, connector=lambda host, port, timeout=0: False, out=out)
    return rc, out.getvalue(), posted


@pytest.mark.parametrize("status", [200, 202, 400, 401, 404, 405])
def test_a_webhook_path_the_app_answers_passes(tmp_path, status):
    rc, text, posted = webhook_go(tmp_path, lambda url: (status, ""))
    assert rc == 0 and "❌" not in text
    assert sorted(posted) == ["https://web.example.com/api/ingest/discord", "https://web.example.com/api/webhooks/clerk"]
    assert f"web.example.com/api/webhooks/clerk reaches the app without a login ({status})" in text


def test_a_webhook_path_refused_by_the_tunnel_fails(tmp_path):
    """Today's paisa state: the Bypass app lets the request through Access, cloudflared still demands a JWT."""
    rc, text, _ = webhook_go(tmp_path, lambda url: (403, ""))
    assert rc == 1 and "web.example.com/api/webhooks/clerk should reach the app without a login" in text
    assert "403: the tunnel still requires Access" in text


def test_a_webhook_path_sent_to_the_access_login_fails(tmp_path):
    rc, text, _ = webhook_go(tmp_path, lambda url: (302, ACCESS))
    assert rc == 1 and "is sent to the Access login" in text


def test_a_webhook_path_that_does_not_answer_fails(tmp_path):
    rc, text, _ = webhook_go(tmp_path, lambda url: (0, "timed out"))
    assert rc == 1 and "got no answer timed out" in text


def test_a_public_app_with_webhook_paths_is_probed_too(tmp_path):
    rc, text, posted = webhook_go(tmp_path, lambda url: (401, ""), access="public", paths=("/hook",))
    assert rc == 0 and posted == ["https://web.example.com/hook"]


def test_an_app_without_webhook_paths_posts_nothing(tmp_path):
    rc, _, posted = webhook_go(tmp_path, lambda url: (403, ""), paths=())
    assert rc == 0 and posted == []


def test_the_webhook_probe_posts_without_credentials(monkeypatch):
    sent = []

    class Opener:
        def open(self, request, timeout=0):
            sent.append(request)
            raise verify.urllib.error.HTTPError(request.full_url, 401, "no", None, None)

    monkeypatch.setattr(verify.urllib.request, "build_opener", lambda *handlers: Opener())
    assert verify.post("https://web.example.com/api/webhooks/clerk") == (401, "")
    (request,) = sent
    assert request.get_method() == "POST" and request.data == b"{}"
    assert {name.lower() for name in request.headers} == {"user-agent", "content-type"}
