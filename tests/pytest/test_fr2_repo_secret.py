"""FR-2: the Dokploy key reaches app repos as a repository secret (works on GitHub Free private repos)."""
from __future__ import annotations

import io
import re
from pathlib import Path

import yaml

from sealedstack import forge, publishsettings
from sealedstack.http import Route, StubClient

from test_engine_publishsettings import FakeSource, FakeState, make_config

REPO = Path(__file__).resolve().parents[2]


def publish(tmp_path, f):
    out = io.StringIO()
    publishsettings.run(make_config(tmp_path), {}, source=FakeSource({"DOKPLOY_API_KEY": "dk"}), state=FakeState(), forge=f, out=out)
    return out.getvalue()


def test_environments_available_still_writes_a_repository_secret(tmp_path):
    f = forge.InMemoryForge()
    log = publish(tmp_path, f)
    assert f.secrets == {("example/shop", "SEALEDSTACK_DOKPLOY_API_KEY"): "dk"}
    assert "mode: repository secret; environments are available" in log


def test_no_environments_on_the_plan_falls_back_cleanly_and_says_so(tmp_path):
    f = forge.InMemoryForge(no_environments={"example/shop"})  # a private repo on GitHub Free
    log = publish(tmp_path, f)
    assert f.secrets == {("example/shop", "SEALEDSTACK_DOKPLOY_API_KEY"): "dk"}
    assert ("example/shop", "production") not in f.environments
    assert "mode: repository secret; environments are not available" in log and "GitHub Free" in log


def test_the_github_forge_writes_repository_secrets_and_survives_a_plan_without_environments():
    http = StubClient()
    http.add("/environments/production", Route(status=403, json_body={"message": "Upgrade to GitHub Pro or make this repository public to enable this feature."}))
    http.add("/actions/secrets/public-key", Route(json_body={"key": "a2V5", "key_id": "k1"}))
    http.add("/actions/secrets/SEALEDSTACK_DOKPLOY_API_KEY", Route(status=201))
    gh = forge.GitHubForge("github_pat_x", http=http, seal=lambda key, value: f"sealed({value})")
    assert gh.ensure_environment("o/shop", "production") is False
    gh.set_secret("o/shop", "SEALEDSTACK_DOKPLOY_API_KEY", "dk")
    put = [r for r in http.requests if r["method"] == "PUT" and "/actions/secrets/" in r["url"]]
    assert put[0]["url"] == "https://api.github.com/repos/o/shop/actions/secrets/SEALEDSTACK_DOKPLOY_API_KEY"
    assert put[0]["json_body"] == {"encrypted_value": "sealed(dk)", "key_id": "k1"}
    assert not any("/environments/production/secrets" in r["url"] for r in http.requests)


def test_secret_exists_distinguishes_missing_from_unreadable():
    http = StubClient()
    http.add("/actions/secrets/A", Route(json_body={"name": "A"}))
    http.add("/actions/secrets/B", Route(status=404, json_body={}))
    http.add("/actions/secrets/C", Route(status=403, json_body={}))
    gh = forge.GitHubForge("github_pat_x", http=http, seal=None)
    assert [gh.secret_exists("o/r", n) for n in "ABC"] == [True, False, None]


def test_the_release_job_reads_the_secret_without_an_environment():
    """The release job declares no `environment:` (it could not see an environment secret) and uses
    only `secrets.SEALEDSTACK_DOKPLOY_API_KEY`, which a repository secret satisfies."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
    release = workflow["jobs"]["release"]
    assert "environment" not in release
    text = (REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    assert "secrets.SEALEDSTACK_DOKPLOY_API_KEY" in text
    assert not re.search(r"^\s+environment:", text, re.M)
