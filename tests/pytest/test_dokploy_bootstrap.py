"""Task 7: Dokploy first-boot automation against a mocked Dokploy API (v0.30.8 endpoints)."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from fake_generated import fake_generated  # noqa: E402
from sealedstack import cli, config as cfg, dokploy, dokploybootstrap, generated as generated_mod, orchestrate  # noqa: E402
from sealedstack.http import HttpClient, Response  # noqa: E402
from sealedstack.secrets import EnvSource  # noqa: E402

ADMIN_PASSWORD = "example-admin-password-0123456789"
NEW_KEY = "dk_example_created_key"


class FakeDokploy(HttpClient):
    """The slice of Dokploy v0.30.8 the bootstrap uses."""

    def __init__(self, *, admin_exists=False, admin_password=ADMIN_PASSWORD, ready_after=0):
        self.admin_exists = admin_exists
        self.admin_password = admin_password
        self.keys = set()
        self.calls = []
        self.not_ready = ready_after

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        headers = headers or {}
        path = url.split(":3000", 1)[1]
        self.calls.append((method, path, dict(headers), json_body))

        def ok(body, cookies=None):
            return Response(200, {}, json.dumps(body).encode(), url, set_cookies=cookies or [])

        if path == "/api/settings.health":
            if self.not_ready > 0:
                self.not_ready -= 1
                raise OSError("connection refused")
            return ok({"ok": True})
        if path == "/api/auth/sign-up/email":
            if self.admin_exists:
                return Response(400, {}, b'{"message":"Admin is already created"}', url)
            self.admin_exists = True
            self.admin_password = json_body["password"]
            return ok({"user": {}}, ["better-auth.session_token=tok123; Path=/; HttpOnly", "better-auth.session_data=d; Path=/"])
        if path == "/api/auth/sign-in/email":
            if self.admin_exists and json_body["password"] == self.admin_password:
                return ok({"ok": 1}, ["better-auth.session_token=tok456; Path=/"])
            return Response(401, {}, b"{}", url)
        if path == "/api/user.session":
            if "Cookie" not in headers:
                return ok(None)
            return ok({"user": {"id": "u1"}, "session": {"activeOrganizationId": "org1"}})
        if path == "/api/user.createApiKey":
            assert "Cookie" in headers and json_body["metadata"] == {"organizationId": "org1"}
            self.keys.add(NEW_KEY)
            return ok({"id": "k1", "key": NEW_KEY, "name": json_body["name"]})
        if path == "/api/project.all":
            return ok([]) if headers.get("x-api-key") in self.keys else Response(401, {}, b"{}", url)
        return Response(404, {}, b"{}", url)


def make_config(tmp_path):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump({"name": "x", "owner_email": "you@example.com", "mesh": {"tailnet": "tail1234.ts.net"},
                                                     "slots": {"secrets": "env"}}))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"server-1": {"provider": "any-ssh", "role": "control"}}}))
    (cdir / "apps.yaml").write_text("apps: {}\n")
    return cfg.load_config(cdir, REPO, {})


def run(config, http, gen, *, source=None, sleep=lambda s: None):
    out = io.StringIO()
    result = dokploybootstrap.run(config, {}, source=source or EnvSource({}), generated=gen, http=http, sleep=sleep, out=out)
    return result, out.getvalue()


def test_first_boot_creates_the_admin_and_an_api_key_and_stores_it_in_the_state(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(), fake_generated()
    result, log = run(config, http, gen)
    assert result == "created"
    assert gen.api_key() == NEW_KEY
    sign_up = next(c for c in http.calls if c[1] == "/api/auth/sign-up/email")
    assert sign_up[3]["email"] == "you@example.com" and sign_up[3]["password"] == ADMIN_PASSWORD
    assert sign_up[0] == "POST" and http.calls[0][1] == "/api/settings.health"
    assert any(c[1] == "/api/project.all" and c[2].get("x-api-key") == NEW_KEY for c in http.calls)  # proven to work
    # the endpoints talk to the control server by its tailnet FQDN
    assert dokploy.control_host(config) == "server-1.tail1234.ts.net"


def test_a_rerun_reuses_the_key_from_the_state_and_creates_nothing(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(), fake_generated()
    run(config, http, gen)
    http.calls.clear()
    result, _ = run(config, http, gen)
    assert result == "state"
    paths = [c[1] for c in http.calls]
    assert "/api/auth/sign-up/email" not in paths and "/api/user.createApiKey" not in paths


def test_a_fresh_workdir_still_finds_the_key_because_it_lives_in_the_state(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(), fake_generated()
    run(config, http, gen)
    again = generated_mod.Generated(gen.backend)  # a new process: nothing cached
    http.calls.clear()
    assert run(config, http, again)[0] == "state"
    assert again.api_key() == NEW_KEY


def test_an_interrupted_run_signs_in_with_the_stored_password(tmp_path):
    config, gen = make_config(tmp_path), fake_generated()
    http = FakeDokploy(admin_exists=True, admin_password=ADMIN_PASSWORD)  # created before the key was stored
    result, log = run(config, http, gen)
    assert result == "created" and "signed in as the admin created earlier" in log


def test_a_foreign_admin_gives_the_exact_manual_step(tmp_path):
    config, gen = make_config(tmp_path), fake_generated()
    http = FakeDokploy(admin_exists=True, admin_password="someone-elses")
    with pytest.raises(dokploybootstrap.BootstrapError) as err:
        run(config, http, gen)
    assert "DOKPLOY_API_KEY" in str(err.value) and "Settings, Profile, API/CLI Keys" in str(err.value)
    assert gen.api_key() == ""


def test_an_override_in_the_secret_source_skips_everything(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(), fake_generated()
    result, _ = run(config, http, gen, source=EnvSource({"DOKPLOY_API_KEY": "manual-key"}))
    assert result == "secret-source" and http.calls == []
    assert generated_mod.resolve_api_key(config, {}, EnvSource({"DOKPLOY_API_KEY": "manual-key"}), gen) == "manual-key"


def test_it_waits_for_the_panel(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(ready_after=3), fake_generated()
    sleeps = []
    assert run(config, http, gen, sleep=sleeps.append)[0] == "created"
    assert len(sleeps) == 3


def test_a_panel_that_never_answers_fails_with_the_fix(tmp_path):
    config, http, gen = make_config(tmp_path), FakeDokploy(ready_after=10_000), fake_generated()
    with pytest.raises(dokploybootstrap.BootstrapError) as err:
        run(config, http, gen)
    assert "tailnet" in str(err.value) and "up" in str(err.value)


def test_no_password_or_key_appears_in_logs_or_outputs(tmp_path, capsys):
    config, http, gen = make_config(tmp_path), FakeDokploy(), fake_generated()
    _, log = run(config, http, gen)
    seen = log + capsys.readouterr().out + capsys.readouterr().err
    for secret in (ADMIN_PASSWORD, NEW_KEY, "tok123"):
        assert secret not in seen


def test_the_stored_values_survive_other_writes(tmp_path):
    gen = fake_generated()
    gen.store(dokploy_api_key="k1")
    gen.store(admin_shown="true")
    assert gen.values() == {"dokploy_api_key": "k1", "admin_shown": "true"}
    assert gen.backend.applied[-1] == {"dokploy_api_key": "k1", "admin_shown": "true"}


def test_the_password_and_keys_are_created_once(tmp_path):
    gen = fake_generated(seeded=False)
    first = dict(gen.ensure())
    second = dict(gen.ensure())
    assert first == second and len(gen.backend.applied) == 1


# ------------------------------------------------------------------ show-dokploy-admin
def test_show_admin_is_refused_in_ci_and_without_break_glass(tmp_path, capsys):
    config, gen = make_config(tmp_path), fake_generated()
    out = io.StringIO()
    assert generated_mod.show_admin(config, {}, EnvSource({}), generated=gen, out=out, is_ci=True, break_glass=True) == 2
    assert generated_mod.show_admin(config, {}, EnvSource({}), generated=gen, out=out, is_ci=False, break_glass=False) == 2
    assert out.getvalue() == "" and ADMIN_PASSWORD not in capsys.readouterr().err


def test_show_admin_prints_the_login_exactly_once(tmp_path, capsys):
    config, gen = make_config(tmp_path), fake_generated()
    out = io.StringIO()
    assert generated_mod.show_admin(config, {}, EnvSource({}), generated=gen, out=out, is_ci=False, break_glass=True) == 0
    assert "you@example.com" in out.getvalue() and ADMIN_PASSWORD in out.getvalue()
    out2 = io.StringIO()
    assert generated_mod.show_admin(config, {}, EnvSource({}), generated=gen, out=out2, is_ci=False, break_glass=True) == 1
    assert out2.getvalue() == ""


def test_cli_status_flag_in_ci_is_refused(tmp_path, capsys):
    config = make_config(tmp_path)
    rc = cli.run(["status", "--show-dokploy-admin", "--break-glass"], code_root=REPO,
                 env={"SEALEDSTACK_CONFIG": str(config.config_dir), "GITHUB_ACTIONS": "true"})
    assert rc == 2
    assert "never reach a job log" in capsys.readouterr().err


# ------------------------------------------------------------------ wiring
def test_up_runs_generated_after_state_and_bootstrap_between_server_and_platform():
    names = [p.name for p in orchestrate.phases("up", code_root=REPO, servers=["server-1"])]
    assert names.index("state-init") < names.index("generated") < names.index("edge-apply")
    assert names.index("server-up:server-1") < names.index("dokploy-bootstrap") < names.index("platform-apply")


def test_the_dokploy_client_uses_the_state_key_without_a_secret(tmp_path):
    config, gen = make_config(tmp_path), fake_generated({"dokploy_api_key": "state-key"})
    # the process-wide cache is keyed by config dir: seed it like a phase would
    generated_mod._CACHE[str(config.config_dir)] = gen
    try:
        client = dokploy.from_config(config, {}, EnvSource({}))
    finally:
        generated_mod._CACHE.pop(str(config.config_dir), None)
    assert client.api_key == "state-key" and client.endpoint == "http://server-1.tail1234.ts.net:3000"


def test_no_key_anywhere_explains_that_up_creates_it(tmp_path):
    config, gen = make_config(tmp_path), fake_generated()
    generated_mod._CACHE[str(config.config_dir)] = gen
    try:
        with pytest.raises(dokploy.DokployError, match="sealedstack up"):
            dokploy.from_config(config, {}, EnvSource({}))
    finally:
        generated_mod._CACHE.pop(str(config.config_dir), None)


def test_dokploy_secrets_are_no_longer_written_to_or_required_from_the_secret_source():
    registry = yaml.safe_load((REPO / "registry" / "platform.yaml").read_text())
    entry = next(e for e in registry["implementations"] if e["id"] == "dokploy")
    assert "DOKPLOY_API_KEY" not in (entry.get("secrets") or []) and "DOKPLOY_API_KEY" in entry["optional_secrets"]
    assert not (REPO / "engine" / "sealedstack" / "dokploykey.py").exists()
    for rel in ("engine/sealedstack/generated.py", "engine/sealedstack/dokploybootstrap.py"):
        text = (REPO / rel).read_text()
        assert "bws" not in text and "secret create" not in text


def test_the_generated_stack_is_encrypted_and_has_no_provider():
    stack = REPO / "tofu" / "stacks" / "generated"
    assert "aes_gcm" in (stack / "encryption.tf").read_text()
    main = (stack / "main.tf").read_text() + (stack / "versions.tf").read_text()
    assert 'provider "' not in main
    outputs = (stack / "outputs.tf").read_text()
    for name in ("dokploy_admin_password", "ci_ssh_private_key", "dokploy_ssh_private_key", "values"):
        assert f'output "{name}"' in outputs
    for sensitive in ("dokploy_admin_password", "ci_ssh_private_key", "dokploy_ssh_private_key", "values"):
        block = outputs.split(f'output "{sensitive}"', 1)[1].split("}", 1)[0]
        assert "sensitive = true" in block


# ------------------------------------------------------------------ the real backend, driven through a fake `tofu`
def test_the_tofu_backend_applies_the_whole_values_map_and_reads_outputs(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    (tmp_path / "tofu" / "stacks" / "generated").mkdir(parents=True)
    calls = []

    def run(argv, env):
        calls.append((list(argv), dict(env)))
        if "output" in argv:
            return 0, json.dumps({"values": {"sensitive": True, "value": {"dokploy_api_key": "k"}},
                                  "ci_ssh_public_key": {"value": "ssh-ed25519 AAAA"}})
        return 0, ""

    monkeypatch.setattr(generated_mod.tofu_mod, "ready", lambda *a, **k: (k["runner"](["tofu", "init"], {}), "0123456789abcdef")[1])
    backend = generated_mod.TofuBackend(config, {"X": "1"}, EnvSource({}), run=run)
    gen = generated_mod.Generated(backend)
    assert gen.values() == {"dokploy_api_key": "k"}
    backend.apply({"dokploy_api_key": "k", "admin_shown": "true"})
    apply_call = next(c for c in calls if "apply" in c[0])
    assert apply_call[0][-3:] == ["-auto-approve", "-input=false", "-no-color"]
    assert json.loads(apply_call[1]["TF_VAR_values"]) == {"dokploy_api_key": "k", "admin_shown": "true"}
    assert apply_call[1]["TF_VAR_state_passphrase"] == "0123456789abcdef"
    assert str(backend.dir).endswith("tofu/stacks/generated")


def test_platform_tfvars_take_the_worker_key_from_the_state(tmp_path):
    from sealedstack import platform

    config = make_config(tmp_path)
    gen = fake_generated({"dokploy_api_key": "state-key"})
    variables = platform.tf_vars(config, {}, EnvSource({}), generated=gen)
    assert variables["TF_VAR_dokploy_api_key"] == "state-key"
    assert variables["TF_VAR_dokploy_ssh_public_key"].startswith("ssh-ed25519")
    assert variables["TF_VAR_dokploy_ssh_private_key"] == "fake-private-key-dokploy\n"


def test_bootstrap_checks_the_keyed_login_with_the_ci_identity():
    text = (REPO / "ansible" / "playbooks" / "bootstrap.yml").read_text()
    assert "admin_ssh_identity" in text and "IdentitiesOnly=yes" in text
