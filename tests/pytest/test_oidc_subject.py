"""The app repo's Actions OIDC subject must be the id form the tag:release credential matches (D-091)."""
from __future__ import annotations

import io
from types import SimpleNamespace

from sealedstack import forge as forge_mod, preflight, publishsettings
from sealedstack.http import Route, StubClient

PATH = "/repos/me/paisa/actions/oidc/customization/sub"


def check(route):
    http = StubClient().add(PATH, route)
    return preflight._check_oidc_subject(SimpleNamespace(http=http), {}, "paisa", "me/paisa")


def test_the_id_form_passes():
    result = check(Route(json_body={"use_default": True, "use_immutable_subject": True, "sub_claim_prefix": "repo:me@1/paisa@2"}))
    assert result.status == "ok" and "repo:me@1/paisa@2" in result.detail


def test_the_name_form_warns_with_the_command():
    result = check(Route(json_body={"use_default": True, "use_immutable_subject": False}))
    assert result.status == "warn" and "name-based" in result.detail and "403" in result.detail
    assert "use_immutable_subject=true" in result.fix


def test_a_custom_subject_warns():
    result = check(Route(json_body={"use_default": False, "include_claim_keys": ["repo"]}))
    assert result.status == "warn" and "custom subject" in result.detail


def test_an_unreadable_setting_names_the_permission():
    result = check(Route(status=403, json_body={"message": "Resource not accessible"}))
    assert result.status == "info" and "Actions: read and write" in result.fix


def github_forge(current):
    http = StubClient().add(PATH, Route(json_body=current))
    return forge_mod.GitHubForge("token", http=http), http


def test_ensure_sets_only_when_needed():
    forge, http = github_forge({"use_default": True, "use_immutable_subject": False})
    assert forge.ensure_id_subject("me/paisa") is True
    put = [r for r in http.requests if r["method"] == "PUT"]
    assert put and put[0]["json_body"] == {"use_default": True, "use_immutable_subject": True}

    forge, http = github_forge({"use_default": True, "use_immutable_subject": True})
    assert forge.ensure_id_subject("me/paisa") is False
    assert not [r for r in http.requests if r["method"] == "PUT"]


def test_publish_warns_instead_of_failing_when_the_token_cannot():
    class NoActions(forge_mod.InMemoryForge):
        def ensure_id_subject(self, repo):
            raise forge_mod.ForgeError("HTTP 403")

    out = io.StringIO()
    publishsettings._ensure_id_subject(NoActions(), "me/paisa", out)
    assert "warn" in out.getvalue() and "gh api -X PUT repos/me/paisa/actions/oidc/customization/sub" in out.getvalue()

    out, forge = io.StringIO(), forge_mod.InMemoryForge()
    publishsettings._ensure_id_subject(forge, "me/paisa", out)
    assert "set to the id form" in out.getvalue() and "me/paisa" in forge.id_subjects
