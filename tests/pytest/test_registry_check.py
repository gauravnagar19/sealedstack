"""`check` probes the registry pull token, and the registry owner follows the app repository (live run 2026-11-01).

The first release of a private-repo app failed with `unauthorized` because the server had no pull token, and the first token
tried was a fine-grained one: GitHub accepts it, ghcr.io returns 403 for every image.
"""
from __future__ import annotations

import copy
import sys

from sealedstack import preflight
from sealedstack.http import Route, StubClient

from test_engine_preflight import APPS, STACK, make_config, make_ctx
from test_engine_server import REPO

sys.path.insert(0, str(REPO / "scripts"))
import render  # noqa: E402

REPO_APPS = {"apps": {"demo-app": {"source": {"repo": "Owner-One/demo-app", "ref": "main"}, "servers": ["web-1"],
                                    "hostnames": ["demo.example.com"], "access": "owner-only"}}}
CLASSIC = "ghp_" + "a" * 36
FINE = "github_pat_" + "b" * 60


def run(tmp_path, token, route=None, apps=REPO_APPS):
    config = make_config(tmp_path, apps=apps)
    http = StubClient().add("api.github.com/user", route or Route(200, json_body={}, headers={"X-OAuth-Scopes": "read:packages"}))
    env = {"REGISTRY_PULL_TOKEN": token} if token else {}
    results = preflight.check_registry(make_ctx(tmp_path, config, env=env, http=http))
    return results[0] if results else None


def test_a_classic_token_with_read_packages_passes(tmp_path):
    result = run(tmp_path, CLASSIC)
    assert result.status == "ok" and "read:packages" in result.detail and CLASSIC not in result.detail


def test_a_missing_token_fails_with_the_prefilled_link(tmp_path):
    result = run(tmp_path, "")
    assert result.status == "fail" and "scopes=read:packages" in result.fix and "demo-app" in result.detail


def test_a_fine_grained_token_is_refused_with_the_reason(tmp_path):
    result = run(tmp_path, FINE, Route(200, json_body={}, headers={}))
    assert result.status == "fail" and "fine-grained" in result.detail and FINE not in result.detail + result.fix


def test_a_token_without_a_package_scope_fails(tmp_path):
    result = run(tmp_path, CLASSIC, Route(200, json_body={}, headers={"x-oauth-scopes": "gist"}))
    assert result.status == "fail" and "read:packages" in result.detail


def test_a_revoked_token_fails(tmp_path):
    assert run(tmp_path, CLASSIC, Route(401, json_body={})).status == "fail"


def test_nothing_is_checked_without_a_repo_app_or_for_another_registry(tmp_path):
    assert run(tmp_path, "", apps=APPS) is None  # image-only apps pull public images
    other = copy.deepcopy(STACK)
    other["slots"]["registry"] = "quay"
    config = make_config(tmp_path / "q", stack=other, apps=REPO_APPS)
    assert preflight.check_registry(make_ctx(tmp_path, config)) == []


def test_the_registry_is_in_the_area_checks():
    assert preflight.check_registry in preflight.AREA_CHECKS


def test_the_owner_defaults_to_the_owner_of_the_first_repo_app():
    registry = render.resolve_registry({"slots": {"registry": "ghcr"}}, REPO_APPS)
    assert registry["owner"] == "owner-one" and registry["image_prefix"] == "ghcr.io/owner-one"
    assert registry["username"] == "owner-one"


def test_a_configured_owner_wins_and_image_apps_do_not_set_one():
    stack = {"slots": {"registry": "ghcr"}, "providers": {"registry_ghcr": {"owner": "Configured"}}}
    assert render.resolve_registry(stack, REPO_APPS)["owner"] == "Configured"
    assert render.resolve_registry({"slots": {"registry": "ghcr"}}, APPS)["owner"] == ""


# -- the exact OIDC subject (live run 2026-11-01: GitHub's subject carries numeric ids) -------------------------------------
REPO_JSON = {"id": 1441299511, "full_name": "gauravnagar19/demo-app", "default_branch": "main", "owner": {"id": 19661833, "login": "gauravnagar19"}}


def subject_ctx(tmp_path, route):
    config = make_config(tmp_path, apps={"apps": {"demo-app": {"source": {"repo": "gauravnagar19/demo-app", "ref": "main"},
                                                              "servers": ["web-1"], "hostnames": ["demo.example.com"], "access": "owner-only"}}})
    return make_ctx(tmp_path, config, http=StubClient().add("api.github.com/repos/gauravnagar19/demo-app", route))


def test_the_exact_subject_comes_from_the_github_ids(tmp_path):
    subjects = preflight.release_subjects(subject_ctx(tmp_path, Route(200, json_body=REPO_JSON)))
    assert subjects == [("demo-app", "repo:gauravnagar19@19661833/demo-app@1441299511:ref:refs/heads/main")]


def test_a_missing_release_credential_names_the_subject_and_the_wildcard(tmp_path):
    result = preflight._check_release_identity(subject_ctx(tmp_path, Route(200, json_body=REPO_JSON)))
    assert result.status == "fail"
    assert "repo:gauravnagar19@19661833/demo-app@1441299511:ref:refs/heads/main" in result.fix
    assert "`repo:gauravnagar19@19661833/*:ref:refs/heads/main`" in result.fix


def test_a_failed_lookup_leaves_the_hint_out_instead_of_guessing(tmp_path):
    ctx = subject_ctx(tmp_path, Route(404, json_body={}))
    assert preflight.release_subjects(ctx) == []
    assert "Subject to enter" not in preflight._check_release_identity(ctx).fix


# -- quote-wrapped secret values (live run 2026-11-01: the .env line was "...", the panel password has no quotes) --------------
def test_a_secret_wrapped_in_quotes_is_flagged_but_still_present(tmp_path):
    config = make_config(tmp_path)
    for value in ('"abcdefghijklmnopqrstuvwxyz0123456789"', "'abcdefghijklmnopqrstuvwxyz0123456789'"):
        ctx = make_ctx(tmp_path, config, env={"STATE_PASSPHRASE": value})
        result = preflight._secret_result(ctx, "state_passphrase", "state encryption", min_length=16)
        assert result.status == "warn" and "quote" in result.detail and value not in result.detail + result.fix


def test_a_value_that_only_contains_a_quote_is_fine(tmp_path):
    config = make_config(tmp_path)
    for value in ("abc'defghijklmnopqrstuvwxyz0123", "'half-quoted-value-0123456789abc", '"'):
        ctx = make_ctx(tmp_path, config, env={"STATE_PASSPHRASE": value})
        assert preflight._secret_result(ctx, "state_passphrase", "x").status in ("ok", "fail")
        assert "quote" not in preflight._secret_result(ctx, "state_passphrase", "x").detail
