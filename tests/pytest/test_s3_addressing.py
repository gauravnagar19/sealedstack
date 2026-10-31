"""DR-1: the R2 client must address the bucket.

The earlier tests mocked the sender and never looked at the URL, so a client that
omitted the bucket passed every test. These tests pin the exact URL, host and
canonical request, check the signing code against the published AWS SigV4 S3
examples, and run the client against a strict local fake S3 server.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fake_s3 import ACCESS_KEY, SECRET_KEY, FakeS3
from sealedstack import cli, lock, r2, s3
from sealedstack import config as cfg
from sealedstack.http import Response

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
EMPTY_SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
AWS_ACCESS = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"


# -- the signing algorithm against the AWS documentation examples ------------------

def test_sigv4_matches_the_aws_get_object_example():
    """AWS docs, "Example: GET Object" (Signature Version 4, Authorization header)."""
    canonical, headers = s3.sign_v4(
        "GET", "examplebucket.s3.amazonaws.com", "/test.txt", EMPTY_SHA, {"Range": "bytes=0-9"},
        access_key=AWS_ACCESS, secret_key=AWS_SECRET, region="us-east-1", now=NOW,
    )
    assert canonical == (
        "GET\n/test.txt\n\n"
        "host:examplebucket.s3.amazonaws.com\n"
        "range:bytes=0-9\n"
        f"x-amz-content-sha256:{EMPTY_SHA}\n"
        "x-amz-date:20130524T000000Z\n\n"
        f"host;range;x-amz-content-sha256;x-amz-date\n{EMPTY_SHA}"
    )
    auth = headers["Authorization"]
    assert "Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request" in auth
    assert "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date" in auth
    assert auth.endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")


def test_sigv4_matches_the_aws_put_object_example():
    """AWS docs, "Example: PUT Object": the key `test$file.text` is encoded `test%24file.text`."""
    body = b"Welcome to Amazon S3."
    canonical, headers = s3.sign_v4(
        "PUT", "examplebucket.s3.amazonaws.com", "/test%24file.text", s3._sha256_hex(body),
        {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        access_key=AWS_ACCESS, secret_key=AWS_SECRET, region="us-east-1", now=NOW,
    )
    assert canonical.splitlines()[:2] == ["PUT", "/test%24file.text"]
    assert "x-amz-content-sha256:44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072" in canonical
    assert headers["Authorization"].endswith("Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")


# -- exact URL, host and canonical request (path-style, bucket included) -----------

def _capture():
    seen = []

    def sender(method, url, headers, body):
        seen.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        return Response(status=200, body=b"", url=url)

    return seen, sender


def _client(sender, bucket="sealedstack-state"):
    return s3.S3Client("https://acct123.r2.cloudflarestorage.com", bucket, "AKID", "SECRET", sender=sender, clock=lambda: NOW)


def test_urls_include_the_bucket_for_put_get_and_delete():
    seen, sender = _capture()
    client = _client(sender)
    client.put_if_absent("sealedstack/my-infra/state.lock", b"{}")
    client.get("sealedstack/my-infra/progress-up.json")
    client.delete("sealedstack/my-infra/state.lock")
    assert [(r["method"], r["url"]) for r in seen] == [
        ("PUT", "https://acct123.r2.cloudflarestorage.com/sealedstack-state/sealedstack/my-infra/state.lock"),
        ("GET", "https://acct123.r2.cloudflarestorage.com/sealedstack-state/sealedstack/my-infra/progress-up.json"),
        ("DELETE", "https://acct123.r2.cloudflarestorage.com/sealedstack-state/sealedstack/my-infra/state.lock"),
    ]


def test_canonical_request_signs_the_bucket_path_and_the_host():
    client = _client(lambda *a: Response(status=200, body=b"", url=""))
    assert client.path("a/b.json") == "/sealedstack-state/a/b.json"
    canonical, _ = s3.sign_v4(
        "PUT", client.host, client.path("sealedstack/my-infra/state.lock"), s3._sha256_hex(b"{}"),
        {"If-None-Match": "*", "Content-Type": "application/json"},
        access_key="AKID", secret_key="SECRET", region="auto", now=NOW,
    )
    lines = canonical.split("\n")
    assert lines[0] == "PUT"
    assert lines[1] == "/sealedstack-state/sealedstack/my-infra/state.lock"
    assert lines[2] == ""  # no query string
    assert "host:acct123.r2.cloudflarestorage.com" in lines
    assert "if-none-match:*" in lines
    assert lines[-2] == "content-type;host;if-none-match;x-amz-content-sha256;x-amz-date"


GOLDEN_SIGNATURE = "8f1316faa945aea9e83916afeb9d8415def125a11110d68fb19ca2dd443ae43b"


def test_the_signature_for_a_fixed_request_is_stable():
    """A golden value: any change to what is signed (URI, headers, scope) shows up here."""
    seen, sender = _capture()
    _client(sender).put_if_absent("sealedstack/my-infra/state.lock", b"{}")
    auth = seen[0]["headers"]["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKID/20130524/auto/s3/aws4_request, ")
    assert "SignedHeaders=content-type;host;if-none-match;x-amz-content-sha256;x-amz-date, " in auth
    assert auth.endswith("Signature=" + GOLDEN_SIGNATURE)


def test_the_bucket_is_required_and_must_be_a_single_segment():
    with pytest.raises(ValueError):
        s3.S3Client("https://x.r2.cloudflarestorage.com", "", "a", "b")
    with pytest.raises(ValueError):
        s3.S3Client("https://x.r2.cloudflarestorage.com", "a/b", "a", "b")


def test_keys_are_percent_encoded_once():
    client = _client(lambda *a: None)
    assert client.path("dir/a b$c.json") == "/sealedstack-state/dir/a%20b%24c.json"


# -- against a strict local fake S3 server -----------------------------------------

def _real_client(server, bucket="sealedstack-state", secret=SECRET_KEY):
    return s3.S3Client(server.endpoint, bucket, ACCESS_KEY, secret)


def test_put_get_delete_and_the_conditional_put_end_to_end():
    with FakeS3() as server:
        client = _real_client(server)
        assert client.put_if_absent("sealedstack/my-infra/state.lock", b"one") is True
        assert server.objects("sealedstack-state") == {"sealedstack/my-infra/state.lock": b"one"}
        # the second attempt is refused with HTTP 412 and the object is untouched
        assert client.put_if_absent("sealedstack/my-infra/state.lock", b"two") is False
        assert server.requests[-1]["status"] == 412
        assert server.requests[-1]["headers"]["if-none-match"] == "*"
        assert client.get("sealedstack/my-infra/state.lock") == b"one"
        assert client.get("missing") is None
        client.put("k.json", b"1")
        client.put("k.json", b"2")  # unconditional put replaces
        assert client.get("k.json") == b"2"
        assert "if-none-match" not in server.requests[-2]["headers"]
        client.delete("sealedstack/my-infra/state.lock")
        assert client.get("sealedstack/my-infra/state.lock") is None
        # every request carried the bucket as the first path segment
        assert all(r["path"].startswith("/sealedstack-state/") for r in server.requests)
        assert all(r["status"] in (200, 204, 404, 412) for r in server.requests)


def test_a_client_without_the_right_bucket_is_refused_by_the_server():
    with FakeS3(buckets=("sealedstack-state",)) as server:
        with pytest.raises(s3.S3Error):
            _real_client(server, bucket="some-other-bucket").put_if_absent("sealedstack/my-infra/state.lock", b"x")
        assert server.requests[-1]["status"] == 404  # NoSuchBucket


def test_the_old_bug_is_caught_a_url_without_the_bucket_never_succeeds():
    """The pre-fix client signed and sent `/<key>`; the key's first segment was read as the bucket."""

    class NoBucketClient(s3.S3Client):
        def path(self, key):
            return "/" + key

    with FakeS3() as server:
        client = NoBucketClient(server.endpoint, "sealedstack-state", ACCESS_KEY, SECRET_KEY)
        with pytest.raises(s3.S3Error):
            client.put_if_absent("sealedstack-state.lock", b"x")
        assert server.requests[-1]["status"] == 404
        assert server.objects("sealedstack-state") == {}


def test_the_server_rejects_a_signature_over_a_different_path_than_the_one_sent():
    class MismatchClient(s3.S3Client):
        def url(self, key):  # sends without the bucket while the signature covers it
            return f"{self.endpoint}/{key}"

    with FakeS3() as server:
        client = MismatchClient(server.endpoint, "sealedstack-state", ACCESS_KEY, SECRET_KEY)
        with pytest.raises(s3.S3Error):
            client.put_if_absent("k", b"x")
        assert server.requests[-1]["status"] == 403


def test_wrong_credentials_are_refused():
    with FakeS3() as server:
        with pytest.raises(s3.S3Error):
            _real_client(server, secret="wrong").put_if_absent("k", b"x")
        assert server.requests[-1]["status"] == 403


# -- the CLI wiring: bucket from stack.yaml, lock and progress keys ----------------

def _config(tmp_path, state=None):
    stack = {"name": "my-infra", "cloudflare": {"account_id": "acct123"}}
    if state:
        stack["state"] = state
    return cfg.Config(REPO, tmp_path, stack, {}, {})


@pytest.fixture
def fake_r2(monkeypatch):
    with FakeS3(buckets=("sealedstack-state", "my-bucket")) as server:
        monkeypatch.setattr(cli, "r2_endpoint", lambda account: server.endpoint)
        monkeypatch.setattr(
            r2, "resolve",
            lambda source, config, env, http=None, required=True: r2.R2Credentials(ACCESS_KEY, SECRET_KEY, True),
        )
        yield server


ENV = {"CLOUDFLARE_API_TOKEN": "t", "GITHUB_SHA": "c" * 40}


def test_the_cli_lock_lives_in_the_state_bucket_under_the_infra_name(tmp_path, fake_r2):
    state_lock = cli._state_lock(_config(tmp_path), ENV)
    info = state_lock.acquire()
    assert list(fake_r2.objects("sealedstack-state")) == ["sealedstack/my-infra/state.lock"]
    assert json.loads(fake_r2.objects("sealedstack-state")["sealedstack/my-infra/state.lock"])["id"] == info["id"]
    # a second lock attempt is refused (what the M4 runbook checks against real R2)
    with pytest.raises(lock.LockHeld):
        cli._state_lock(_config(tmp_path), ENV).acquire()
    assert state_lock.release(info) is True
    assert fake_r2.objects("sealedstack-state") == {}


def test_a_custom_state_bucket_is_honoured(tmp_path, fake_r2):
    cli._state_lock(_config(tmp_path, {"bucket": "my-bucket"}), ENV).acquire()
    assert list(fake_r2.objects("my-bucket")) == ["sealedstack/my-infra/state.lock"]
    assert fake_r2.objects("sealedstack-state") == {}


def test_progress_is_stored_in_the_state_bucket(tmp_path, fake_r2):
    store = cli._progress(_config(tmp_path), ENV, "up", str(tmp_path / "w"))
    store.save({"state-init"})
    key = "sealedstack/my-infra/progress-up.json"
    assert json.loads(fake_r2.objects("sealedstack-state")[key]) == {"commit": "c" * 40, "done": ["state-init"]}
    assert store.load() == ({"state-init"}, "")
    store.clear()
    assert fake_r2.objects("sealedstack-state") == {}


def test_lock_key_is_per_infra_repo():
    assert lock.lock_key("my-infra") == "sealedstack/my-infra/state.lock"
    assert lock.lock_key("") == "sealedstack/sealedstack/state.lock"
    assert re.fullmatch(r"sealedstack/[^/]+/state\.lock", lock.lock_key("shop-infra"))
