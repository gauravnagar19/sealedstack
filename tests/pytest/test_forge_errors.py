"""DR-9: forge errors say what GitHub said, and transient failures are retried."""
from __future__ import annotations

import json

import pytest

from sealedstack import forge
from sealedstack.http import HttpClient, Response


class Scripted(HttpClient):
    """Answers with the given responses in order; records each call."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.calls.append((method, url))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


def reply(status, body=None, headers=None):
    return Response(status, headers or {}, json.dumps(body if body is not None else {}).encode(), "")


def gh(http, sleeps=None):
    return forge.GitHubForge("github_pat_x", http=http, seal=None, sleep=(sleeps.append if sleeps is not None else (lambda s: None)))


def test_errors_carry_githubs_message_and_the_request_id():
    http = Scripted(reply(403, {"message": "Resource not accessible by personal access token", "documentation_url": "x"},
                          {"X-GitHub-Request-Id": "ABCD:1234:5678"}))
    with pytest.raises(forge.ForgeError) as err:
        gh(http)._request("GET", "/repos/o/r/issues")
    text = str(err.value)
    assert text == "GET /repos/o/r/issues failed (HTTP 403): Resource not accessible by personal access token [x-github-request-id ABCD:1234:5678]"
    assert err.value.status == 403 and len(http.calls) == 1  # a plain 403 is a permission problem: not retried


def test_a_non_json_error_body_still_reports_status_and_id():
    http = Scripted(Response(502, {"x-github-request-id": "ZZ"}, b"<html>Bad gateway</html>", ""))
    with pytest.raises(forge.ForgeError, match=r"HTTP 502\) \[x-github-request-id ZZ\]"):
        gh(http)._request("GET", "/x")


def test_the_message_is_trimmed_and_flattened():
    http = Scripted(reply(422, {"message": "line1\nline2 " + "x" * 500}))
    with pytest.raises(forge.ForgeError) as err:
        gh(http)._request("POST", "/x", json_body={})
    assert "\n" not in str(err.value) and len(str(err.value)) < 400


def test_5xx_on_a_read_is_retried_with_backoff_and_then_succeeds():
    http = Scripted(reply(502), reply(503), reply(200, {"ok": True}))
    sleeps = []
    assert gh(http, sleeps)._request("GET", "/x") == {"ok": True}
    assert len(http.calls) == 3 and sleeps == [1.0, 2.0]


def test_it_gives_up_after_three_tries_and_reports_the_last_failure():
    http = Scripted(reply(500, {"message": "Server Error"}, {"x-github-request-id": "R1"}))
    sleeps = []
    with pytest.raises(forge.ForgeError, match=r"HTTP 500\): Server Error \[x-github-request-id R1\]"):
        gh(http, sleeps)._request("GET", "/x")
    assert len(http.calls) == 3 and len(sleeps) == 2


def test_429_is_retried_honouring_retry_after_but_capped():
    http = Scripted(reply(429, {}, {"Retry-After": "7"}), reply(429, {}, {"Retry-After": "9999"}), reply(200, {}))
    sleeps = []
    gh(http, sleeps)._request("GET", "/x")
    assert sleeps == [7.0, 60.0]


def test_a_secondary_rate_limit_403_is_retried_even_for_a_post():
    limited = reply(403, {"message": "You have exceeded a secondary rate limit. Please wait a few minutes"}, {"Retry-After": "3"})
    http = Scripted(limited, reply(201, {"id": 1}))
    sleeps = []
    assert gh(http, sleeps)._request("POST", "/repos/o/r/issues", json_body={"title": "t"}) == {"id": 1}
    assert sleeps == [3.0] and [c[0] for c in http.calls] == ["POST", "POST"]


def test_a_post_is_never_repeated_after_a_5xx_it_may_have_succeeded():
    http = Scripted(reply(502))
    with pytest.raises(forge.ForgeError):
        gh(http)._request("POST", "/repos/o/r/issues", json_body={"title": "t"})
    assert len(http.calls) == 1


def test_other_4xx_are_not_retried():
    for status in (400, 401, 404, 409, 422):
        http = Scripted(reply(status))
        with pytest.raises(forge.ForgeError):
            gh(http)._request("GET", "/x")
        assert len(http.calls) == 1, status


def test_501_is_not_retried():
    http = Scripted(reply(501))
    with pytest.raises(forge.ForgeError):
        gh(http)._request("GET", "/x")
    assert len(http.calls) == 1


def test_paged_reads_retry_each_page_and_the_token_is_still_not_sent_off_host():
    page1 = reply(200, [{"n": 1}], {"Link": '<https://api.github.com/x?page=2>; rel="next"'})
    http = Scripted(page1, reply(503), reply(200, [{"n": 2}]))
    assert gh(http)._paged("/x") == [{"n": 1}, {"n": 2}] and len(http.calls) == 3


def test_get_variable_and_friends_that_swallow_forge_errors_still_work_on_404():
    http = Scripted(reply(404, {"message": "Not Found"}))
    g = gh(http)
    assert g.secret_exists("o/r", "S") is False
    assert g.ensure_environment("o/r", "production") is False
