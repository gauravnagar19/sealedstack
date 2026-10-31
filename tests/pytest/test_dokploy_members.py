"""D-063 / FR-1: one scoped Dokploy member per app; the app repo gets that key, never the admin key."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from fake_dokploy import ADMIN_KEY, FakeDokploy
from fake_generated import fake_generated
from sealedstack import config as config_mod, dokploymembers, forge, publishsettings, secrets as secrets_mod
from sealedstack.secrets import SecretResult

REPO = Path(__file__).resolve().parents[2]
COMPOSES = {"c-shop-1": ("p-shop", "e-shop"), "c-shop-2": ("p-shop", "e-shop"), "c-blog": ("p-blog", "e-blog")}
COMPOSE_IDS = {"shop": {"server-1": "c-shop-1", "server-2": "c-shop-2"}, "blog": {"server-1": "c-blog"}}


class Source(secrets_mod.SecretSource):
    kind = "fake"

    def __init__(self, data):
        self.data = data

    def get(self, name):
        return SecretResult(name, "found", self.data[name]) if name in self.data else SecretResult(name, "missing", detail="no")


class State:
    def output(self, name):
        return COMPOSE_IDS if name == "compose_ids" else {}


def make_config(tmp_path, repos=("example/shop", "example/blog")):
    cdir = tmp_path / "config"
    cdir.mkdir(exist_ok=True)
    (cdir / "stack.yaml").write_text(yaml.safe_dump({
        "name": "example-infra", "cloudflare": {"account_id": "acct"}, "mesh": {"tailnet": "tail1234.ts.net"}, "slots": {"secrets": "env"},
        "providers": {"mesh_tailscale": {"release_oauth_client_id": "rel-id", "release_audience": "rel-aud"}}}))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"server-1": {"role": "control"}, "server-2": {}}}))
    (cdir / "apps.yaml").write_text(yaml.safe_dump({"apps": {"shop": {"source": {"repo": repos[0]}}, "blog": {"source": {"repo": repos[1]}}}}))
    return config_mod.load_config(config_dir=cdir, code_root=tmp_path, env={})


def provision(tmp_path, dokploy=None, gen=None, apps=COMPOSE_IDS):
    dokploy = dokploy or FakeDokploy(COMPOSES)
    gen = gen or fake_generated()
    out = io.StringIO()
    keys = dokploymembers.ensure(make_config(tmp_path), apps, admin_key=ADMIN_KEY, generated=gen, http=dokploy, out=out,
                                 new_password=iter(f"member-password-{i:02d}-0123456789ab" for i in range(9)).__next__)
    return keys, dokploy, gen, out.getvalue()


def test_each_app_gets_its_own_member_scoped_to_its_own_composes(tmp_path):
    keys, dokploy, gen, log = provision(tmp_path)
    assert set(keys) == {"shop", "blog"} and keys["shop"] != keys["blog"] and ADMIN_KEY not in keys.values()
    shop = dokploy.users[dokploy.member("deploy-shop@dokploy.invalid")]
    assert shop["role"] == "member"
    assert shop["perms"]["accessedServices"] == ["c-shop-1", "c-shop-2"]
    assert shop["perms"]["accessedProjects"] == ["p-shop"] and shop["perms"]["accessedEnvironments"] == ["e-shop"]
    blog = dokploy.users[dokploy.member("deploy-blog@dokploy.invalid")]
    assert blog["perms"]["accessedServices"] == ["c-blog"] and blog["perms"]["accessedProjects"] == ["p-blog"]
    # nothing else is granted
    for member in (shop, blog):
        granted = {k for k, v in member["perms"].items() if k.startswith("can") and v}
        assert granted == {"canCreateServices"}  # compose.update needs service:create (Dokploy v0.30.8)
        assert member["perms"]["accessedGitProviders"] == [] and member["perms"]["accessedServers"] == []


def test_a_member_key_works_on_its_own_app_and_is_refused_on_others(tmp_path):
    keys, dokploy, *_ = provision(tmp_path)
    for path in ("/api/compose.one?composeId=c-shop-1", "/api/deployment.allByCompose?composeId=c-shop-2"):
        assert dokploy.request("GET", f"http://h:3000{path}", headers={"x-api-key": keys["shop"]}).status == 200
    for path in ("/api/compose.one?composeId=c-blog", "/api/deployment.allByCompose?composeId=c-blog"):
        assert dokploy.request("GET", f"http://h:3000{path}", headers={"x-api-key": keys["shop"]}).status == 401
    ok = dokploy.request("POST", "http://h:3000/api/compose.update", headers={"x-api-key": keys["shop"]}, json_body={"composeId": "c-shop-1", "env": "A=1"})
    assert ok.status == 200
    denied = dokploy.request("POST", "http://h:3000/api/compose.deploy", headers={"x-api-key": keys["shop"]}, json_body={"composeId": "c-blog"})
    assert denied.status == 401
    # no panel administration: no user management, only its own project in the project list
    assert dokploy.request("GET", "http://h:3000/api/user.all", headers={"x-api-key": keys["shop"]}).status == 401
    projects = dokploy.request("GET", "http://h:3000/api/project.all", headers={"x-api-key": keys["shop"]}).json()
    assert [p["projectId"] for p in projects] == ["p-shop"]


def test_keys_and_passwords_are_kept_in_the_encrypted_state_only(tmp_path):
    keys, dokploy, gen, log = provision(tmp_path)
    values = gen.values()
    assert values["dokploy_member_shop_key"] == keys["shop"] and values["dokploy_member_blog_key"] == keys["blog"]
    assert values["dokploy_member_shop_password"].startswith("member-password-")
    for secret in (*keys.values(), values["dokploy_member_shop_password"], ADMIN_KEY):
        assert secret not in log  # never printed


def test_a_rerun_creates_nothing_new_but_reasserts_the_permissions(tmp_path):
    keys, dokploy, gen, _ = provision(tmp_path)
    dokploy.calls.clear()
    again, *_ = provision(tmp_path, dokploy, gen)
    assert again == keys
    paths = [c[1] for c in dokploy.calls]
    assert "/api/user.createUserWithCredentials" not in paths and "/api/user.createApiKey" not in paths
    assert paths.count("/api/user.assignPermissions") == 2
    # a new server (a new compose) is added to the member's services on the next run
    dokploy.composes["c-shop-3"] = ("p-shop", "e-shop")
    ids = {**COMPOSE_IDS, "shop": {**COMPOSE_IDS["shop"], "server-3": "c-shop-3"}}
    provision(tmp_path, dokploy, gen, apps=ids)
    assert "c-shop-3" in dokploy.users[dokploy.member("deploy-shop@dokploy.invalid")]["perms"]["accessedServices"]


def test_a_key_that_stopped_working_is_replaced(tmp_path):
    keys, dokploy, gen, _ = provision(tmp_path)
    del dokploy.keys[keys["shop"]]  # revoked in the panel
    again, *_ = provision(tmp_path, dokploy, gen)
    assert again["shop"] != keys["shop"] and again["blog"] == keys["blog"]
    assert gen.values()["dokploy_member_shop_key"] == again["shop"]


def test_an_over_broad_key_is_never_returned(tmp_path):
    """If the panel ever hands out a key that reaches another app, refuse to publish it."""
    dokploy = FakeDokploy(COMPOSES)
    real_assign = dokploy.request

    def widen(method, url, **kw):
        if url.endswith("/api/user.assignPermissions"):
            kw["json_body"] = {**kw["json_body"], "accessedServices": list(COMPOSES)}  # simulate a panel granting everything
        return real_assign(method, url, **kw)

    dokploy.request = widen
    with pytest.raises(dokploymembers.MemberError, match="does not have the expected scope"):
        provision(tmp_path, dokploy)


def test_an_interrupted_run_signs_in_with_the_stored_password(tmp_path):
    dokploy, gen = FakeDokploy(COMPOSES), fake_generated()
    calls = {"n": 0}
    real = dokploy.request

    def flaky(method, url, **kw):
        if url.endswith("/api/user.createApiKey"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("connection reset")
        return real(method, url, **kw)

    dokploy.request = flaky
    with pytest.raises(OSError):
        provision(tmp_path, dokploy, gen)
    assert gen.values()["dokploy_member_shop_password"]  # stored before the user was created
    keys, *_ = provision(tmp_path, dokploy, gen)  # the retry reuses the member and its password
    assert len([u for u in dokploy.users.values() if u["email"] == "deploy-shop@dokploy.invalid"]) == 1
    assert keys["shop"]


def test_an_app_without_a_compose_yet_is_skipped_with_a_message(tmp_path):
    keys, dokploy, gen, log = provision(tmp_path, apps={"shop": {}, "blog": {"server-1": "c-blog"}})
    assert set(keys) == {"blog"} and "shop: no compose exists yet" in log


# -- the published secret ------------------------------------------------------------
def publish(tmp_path, f, dokploy, gen):
    publishsettings.run(make_config(tmp_path), {}, source=Source({"DOKPLOY_API_KEY": ADMIN_KEY}), state=State(), forge=f,
                        generated=gen, http=dokploy, out=io.StringIO())


def test_publish_gives_each_app_repo_its_member_key_and_never_the_admin_key(tmp_path):
    f, dokploy, gen = forge.InMemoryForge(), FakeDokploy(COMPOSES), fake_generated()
    publish(tmp_path, f, dokploy, gen)
    assert f.secrets[("example/shop", "SEALEDSTACK_DOKPLOY_API_KEY")] == gen.values()["dokploy_member_shop_key"]
    assert f.secrets[("example/blog", "SEALEDSTACK_DOKPLOY_API_KEY")] == gen.values()["dokploy_member_blog_key"]
    # the admin key is nowhere in anything written to an app repo (variables, secrets, comments, files)
    written = json.dumps([[list(k) + [v] for k, v in store.items()] for store in (f.secrets, f.variables, f.files)] + [f.issues], default=str)
    assert ADMIN_KEY not in written
    assert ADMIN_KEY not in " ".join(f.secrets.values())


def test_the_published_key_is_scoped_in_the_panel(tmp_path):
    f, dokploy, gen = forge.InMemoryForge(), FakeDokploy(COMPOSES), fake_generated()
    publish(tmp_path, f, dokploy, gen)
    shop_key = f.secrets[("example/shop", "SEALEDSTACK_DOKPLOY_API_KEY")]
    assert dokploy.request("GET", "http://h:3000/api/compose.one?composeId=c-blog", headers={"x-api-key": shop_key}).status == 401
    assert dokploy.request("GET", "http://h:3000/api/compose.one?composeId=c-shop-1", headers={"x-api-key": shop_key}).status == 200


def test_publish_fails_closed_when_the_panel_is_unreachable(tmp_path):
    class Down(FakeDokploy):
        def request(self, *a, **kw):
            raise OSError("no route to host")

    f = forge.InMemoryForge()
    with pytest.raises(OSError):
        publish(tmp_path, f, Down(COMPOSES), fake_generated())
    assert f.secrets == {}  # nothing (certainly not the admin key) was published
