"""Doppler as a native, read-only secret source (D-066).

The source reads one Doppler config with a service token over the REST API (no
``doppler`` CLI in the image). Requests are asserted exactly (URL, header), and a strict
local fake server exercises the real HTTP client, so a wrong URL or header cannot hide
behind a mock (the lesson of the S3 bucket bug, DR-1).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from fake_doppler import FakeDoppler  # noqa: E402
from sealedstack import config as cfg  # noqa: E402
from sealedstack import doppler, preflight, scaffold, secrets, setupjob  # noqa: E402
from sealedstack.http import HttpClient, Response, Route, StubClient, UrllibClient  # noqa: E402

TOKEN = "dp.st.prd." + "a" * 40  # obviously fake: low entropy, so secret scanners ignore it
APPS_TOKEN = "dp.st.prd." + "b" * 40
URL = "https://api.doppler.com/v3/configs/config/secrets/download?format=json"


class Seq(HttpClient):
    """Scripted responses in order; records every request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item


def ok(data):
    return Response(200, body=json.dumps(data).encode())


def err(status, *messages, headers=None):
    body = json.dumps({"messages": list(messages), "success": False}).encode()
    return Response(status, headers=headers or {}, body=body)


def source(http, env=None, **kw):
    return doppler.DopplerSource({"DOPPLER_TOKEN": TOKEN, **(env or {})}, http=http, sleep=kw.pop("sleep", lambda s: None), **kw)


# -- naming ------------------------------------------------------------------------------
def test_doppler_names_are_upper_case_letters_digits_and_underscores():
    assert doppler.doppler_name("shop.CLERK_SECRET_KEY") == "SHOP_CLERK_SECRET_KEY"
    assert doppler.doppler_name("state_passphrase") == "STATE_PASSPHRASE"
    assert doppler.doppler_name("my-app.db url") == "MY_APP_DB_URL"
    assert doppler.doppler_name("2fa.KEY") == "_2FA_KEY"  # Doppler forbids a leading digit


def test_the_name_as_written_is_tried_before_the_normalised_one():
    assert doppler.candidates("shop.KEY") == ["shop.KEY", "SHOP_KEY"]
    assert doppler.candidates("STATE_PASSPHRASE") == ["STATE_PASSPHRASE"]


# -- lookups -----------------------------------------------------------------------------
def test_a_secret_is_found_by_its_exact_name():
    s = source(Seq(ok({"CLOUDFLARE_API_TOKEN": "cf"})))
    result = s.get("CLOUDFLARE_API_TOKEN")
    assert result.found and result.value == "cf"


def test_an_app_secret_with_a_dot_is_found_under_the_normalised_name():
    s = source(Seq(ok({"SHOP_CLERK_SECRET_KEY": "sk"})))
    assert s.get("shop.CLERK_SECRET_KEY").value == "sk"


def test_permissive_mode_workspaces_keep_the_dotted_name():
    s = source(Seq(ok({"shop.CLERK_SECRET_KEY": "dotted", "SHOP_CLERK_SECRET_KEY": "other"})))
    assert s.get("shop.CLERK_SECRET_KEY").value == "dotted"


def test_a_missing_secret_lists_the_names_tried_and_never_the_token():
    s = source(Seq(ok({"OTHER": "x"})))
    result = s.get("shop.KEY")
    assert result.missing
    assert "shop.KEY or SHOP_KEY" in result.detail
    assert TOKEN not in result.detail


def test_an_empty_secret_is_reported_as_missing():
    s = source(Seq(ok({"STATE_PASSPHRASE": "", "NULLISH": None})))
    assert s.get("STATE_PASSPHRASE").missing and "empty" in s.get("STATE_PASSPHRASE").detail
    assert s.get("NULLISH").missing


def test_one_request_serves_every_lookup_and_it_is_exactly_the_documented_call():
    http = Seq(ok({"A": "1", "B": "2", "C": "3"}))
    s = source(http)
    assert [s.get(n).value for n in ("A", "B", "C")] == ["1", "2", "3"] and s.get("D").missing
    assert len(http.requests) == 1
    request = http.requests[0]
    assert request["method"] == "GET" and request["url"] == URL
    assert request["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in request["url"]


def test_an_exported_variable_wins_without_any_request():
    http = Seq(ok({}))
    s = source(http, {"STATE_PASSPHRASE": "from-env"})
    assert s.get("STATE_PASSPHRASE").value == "from-env"
    assert s.get("DOPPLER_TOKEN").found  # `check` finds the source's own token this way
    assert http.requests == []


# -- the token ---------------------------------------------------------------------------
def test_no_token_is_one_clear_error_and_no_request():
    http = Seq(ok({}))
    s = doppler.DopplerSource({}, http=http)
    result = s.get("X")
    assert result.errored and "DOPPLER_TOKEN is not set" in result.detail
    assert http.requests == []


def test_only_service_tokens_are_accepted_and_the_value_is_never_echoed():
    http = Seq(ok({}))
    personal = "dp.pt." + "c" * 30
    s = doppler.DopplerSource({"DOPPLER_TOKEN": personal}, http=http)
    result = s.get("X")
    assert result.errored and "service token" in result.detail and "dp.st." in result.detail
    assert personal not in result.detail and http.requests == []


def test_the_infra_source_ignores_the_apps_token():
    http = Seq(ok({"A": "1"}))
    s = doppler.DopplerSource({"DOPPLER_TOKEN": TOKEN, "DOPPLER_TOKEN_APPS": APPS_TOKEN}, http=http)
    s.get("A")
    assert http.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_the_apps_source_prefers_its_own_token_and_falls_back_to_the_infra_one():
    http = Seq(ok({"A": "1"}))
    both = doppler.DopplerSource({"DOPPLER_TOKEN": TOKEN, "DOPPLER_TOKEN_APPS": APPS_TOKEN}, apps=True, http=http)
    both.get("A")
    assert http.requests[0]["headers"]["Authorization"] == f"Bearer {APPS_TOKEN}"

    http2 = Seq(ok({"A": "1"}))
    only_infra = doppler.DopplerSource({"DOPPLER_TOKEN": TOKEN}, apps=True, http=http2)
    assert only_infra.get("A").value == "1"
    assert http2.requests[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    none = doppler.DopplerSource({}, apps=True, http=Seq(ok({})))
    assert "DOPPLER_TOKEN_APPS or DOPPLER_TOKEN" in none.get("A").detail


# -- failures ----------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, needle",
    [(401, "invalid, revoked or expired"), (403, "not allowed to read secrets"), (404, "found no config"), (418, "HTTP 418")],
)
def test_http_failures_are_explained_with_dopplers_own_message(status, needle):
    s = source(Seq(err(status, "Doppler says no")))
    result = s.get("X")
    assert result.errored and needle in result.detail and "Doppler says no" in result.detail
    assert TOKEN not in result.detail


def test_a_rate_limit_waits_for_retry_after_then_succeeds():
    sleeps = []
    http = Seq(err(429, "slow down", headers={"Retry-After": "7"}), ok({"A": "1"}))
    s = source(http, sleep=sleeps.append)
    assert s.get("A").value == "1"
    assert sleeps == [7.0] and len(http.requests) == 2


def test_a_long_retry_after_is_capped():
    sleeps = []
    s = source(Seq(err(429, headers={"retry-after": "9999"}), ok({"A": "1"})), sleep=sleeps.append)
    s.get("A")
    assert sleeps == [doppler.RETRY_AFTER_CAP]


def test_a_persistent_rate_limit_gives_up_after_three_tries_with_a_clear_message():
    sleeps = []
    http = Seq(err(429, "limit"))
    s = source(http, sleep=sleeps.append)
    result = s.get("A")
    assert result.errored and "rate limit" in result.detail
    assert len(http.requests) == 3 and len(sleeps) == 2


def test_a_server_error_is_retried():
    http = Seq(Response(503), ok({"A": "1"}))
    assert source(http).get("A").value == "1" and len(http.requests) == 2


def test_a_client_error_is_not_retried():
    http = Seq(err(401, "bad"))
    source(http).get("A")
    assert len(http.requests) == 1


def test_a_transport_failure_is_an_error_without_the_token():
    http = Seq(OSError("connection refused"))
    result = source(http).get("A")
    assert result.errored and "could not reach Doppler" in result.detail and TOKEN not in result.detail


def test_a_failure_is_remembered_so_a_run_makes_one_failing_call_not_one_per_secret():
    http = Seq(err(401, "bad"))
    s = source(http)
    first = s.get("A")
    again = s.get("B")
    assert first.errored and again.errored and len(http.requests) == 1


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
def test_an_unexpected_body_is_an_error(body):
    assert source(Seq(Response(200, body=body))).get("A").errored


# -- build_source ------------------------------------------------------------------------
def test_build_source_picks_doppler_natively(tmp_path):
    stack = {"slots": {"secrets": "doppler"}}
    built = secrets.build_source(stack, tmp_path, {"DOPPLER_TOKEN": TOKEN})
    assert isinstance(built, doppler.DopplerSource) and built.apps is False


def test_doppler_is_no_longer_a_command_line_backend():
    assert "doppler" not in secrets.CLI_BACKENDS
    legacy = secrets.CliSource("doppler", {"slots": {"secrets": "doppler"}}, Path("."), {}, runner=lambda argv: (0, "x", ""))
    assert legacy.get("ANY").errored  # the old `doppler secrets get` branch is gone


def test_app_secrets_use_the_apps_token(tmp_path):
    stack = {"slots": {"secrets": "doppler"}}
    assert secrets.build_source(stack, tmp_path, {}, project="prod").apps is True
    assert secrets.build_source(stack, tmp_path, {}, project="infra").apps is False
    custom = {"slots": {"secrets": "doppler"}, "secrets": {"project": "core"}}
    assert secrets.build_source(custom, tmp_path, {}, project="core").apps is False
    assert secrets.build_source(custom, tmp_path, {}, project="prod").apps is True


# -- the real HTTP client against the strict fake server ---------------------------------
def real(fake, env=None, sleeps=None):
    return doppler.DopplerSource(
        {"DOPPLER_TOKEN": TOKEN, **(env or {})}, http=UrllibClient(), api=fake.endpoint,
        sleep=(sleeps.append if sleeps is not None else (lambda s: None)),
    )


def test_end_to_end_over_real_http():
    with FakeDoppler({TOKEN: {"STATE_PASSPHRASE": "p" * 20, "SHOP_KEY": "v"}}) as fake:
        s = real(fake)
        assert s.get("STATE_PASSPHRASE").value == "p" * 20
        assert s.get("shop.KEY").value == "v"
        assert s.get("NOPE").missing
    assert len(fake.requests) == 1
    request = fake.requests[0]
    assert request["path"] == "/v3/configs/config/secrets/download" and request["query"] == {"format": ["json"]}
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_end_to_end_a_wrong_token_is_a_401_with_dopplers_message():
    with FakeDoppler({TOKEN: {}}) as fake:
        result = real(fake, {"DOPPLER_TOKEN": "dp.st.prd.WRONG"}).get("A")
    assert result.errored and "HTTP 401" in result.detail and "Invalid Auth token" in result.detail


def test_end_to_end_rate_limit_then_success_reads_the_retry_after_header():
    sleeps = []
    with FakeDoppler({TOKEN: {"A": "1"}}) as fake:
        fake.script.append((429, {"retry-after": "2"}, {"messages": ["slow"], "success": False}))
        assert real(fake, sleeps=sleeps).get("A").value == "1"
    assert sleeps == [2.0] and len(fake.requests) == 2


# -- check, Setup checklist, devcontainer -------------------------------------------------
def make_ctx(tmp_path, *, env, http=None):
    root = tmp_path / "infra"
    (root / "config").mkdir(parents=True)
    stack = {"name": "x", "domain": "example.com", "owner_email": "you@example.com",
             "slots": {"compute": "any-ssh", "mesh": "tailscale", "ingress": "cloudflare", "gate": "cloudflare_access",
                       "secrets": "doppler", "backup": "r2", "platform": "dokploy"},
             "mesh": {"tailnet": "tail1234.ts.net", "policy": "unmanaged"}}
    (root / "config" / "stack.yaml").write_text(yaml.safe_dump(stack))
    (root / "config" / "servers.yaml").write_text("servers: {}\n")
    (root / "config" / "apps.yaml").write_text("apps: {}\n")
    config = cfg.load_config(root / "config", REPO, {})
    src = secrets.build_source(config.stack, config.config_dir, env, http=http)
    return config, preflight.build_context(config, env=env, secrets=src, version="0.6.0", build_dir=root / ".build")


def rows(results):
    return {r.id: r for r in results}


def test_check_says_exactly_what_to_do_when_the_token_is_missing(tmp_path):
    _, ctx = make_ctx(tmp_path, env={})
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "DOPPLER_TOKEN" in row.detail
    assert "service token" in row.fix and "repository secret DOPPLER_TOKEN" in row.fix


def test_check_reports_a_reachable_source_without_calling_it_a_project(tmp_path):
    http = StubClient().add("secrets/download", Route(200, json_body={"STATE_PASSPHRASE": "p" * 20}))
    _, ctx = make_ctx(tmp_path, env={"DOPPLER_TOKEN": TOKEN}, http=http)
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "ok" and "the config of the Doppler service token" in row.detail
    assert "project 'infra'" not in row.detail


def test_check_explains_a_rejected_token(tmp_path):
    http = StubClient().add("secrets/download", Route(401, json_body={"messages": ["Invalid Auth token"], "success": False}))
    _, ctx = make_ctx(tmp_path, env={"DOPPLER_TOKEN": TOKEN}, http=http)
    row = rows(preflight.check_secrets(ctx))["secrets.source"]
    assert row.status == "fail" and "HTTP 401" in row.detail and TOKEN not in row.detail + row.fix


def test_a_missing_secret_names_the_service_token_config_not_a_project(tmp_path):
    http = StubClient().add("secrets/download", Route(200, json_body={"STATE_PASSPHRASE": "p" * 20}))
    _, ctx = make_ctx(tmp_path, env={"DOPPLER_TOKEN": TOKEN}, http=http)
    row = rows(preflight.check_secrets(ctx))["secrets.cloudflare_api_token"]
    assert row.status == "fail" and "the config of the Doppler service token" in row.detail + row.fix
    assert "project 'infra'" not in row.detail + row.fix


def test_setup_accepts_doppler_and_writes_it_into_the_config():
    args = setupjob.parse_args(["--domain", "example.com", "--email", "you@example.com", "--server", "server-1", "--secrets", "doppler"])
    assert args.secrets == "doppler"
    files = setupjob.build_files(args, repo="you/my-infra", version="0.6.0", owner="you")
    stack = yaml.safe_load(files["config/stack.yaml"])
    assert stack["slots"]["secrets"] == "doppler"


def test_the_devcontainer_passes_through_the_doppler_tokens_only_for_doppler():
    doppler_env = json.loads(scaffold.devcontainer_json("ghcr.io/you/sealedstack:0.6.0", "doppler"))["remoteEnv"]
    assert set(doppler_env) == {"DOPPLER_TOKEN", "DOPPLER_TOKEN_APPS", "GH_TOKEN"}
    default_env = json.loads(scaffold.devcontainer_json("ghcr.io/you/sealedstack:0.6.0"))["remoteEnv"]
    assert set(default_env) == {"BWS_ACCESS_TOKEN", "GH_TOKEN"}


def test_the_setup_checklist_names_doppler_not_a_project(tmp_path):
    http = StubClient().add("secrets/download", Route(200, json_body={}))
    config, ctx = make_ctx(tmp_path, env={"DOPPLER_TOKEN": TOKEN}, http=http)
    results = preflight.check_secrets(ctx)
    items = setupjob.checklist_items(results, config, repo="you/my-infra", owner="you", version="0.6.0")
    text = json.dumps([vars(i) for i in items])
    assert "Doppler, the config of your service token" in text
    assert "project `infra`" not in text


# -- the edge stack no longer needs Bitwarden --------------------------------------------
def test_the_edge_stack_works_without_any_bitwarden_token(tmp_path):
    from sealedstack import edge

    config, _ = make_ctx(tmp_path, env={})
    needed = {"CLOUDFLARE_API_TOKEN": "cf", "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec",
              "R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b"}
    variables = edge.tf_vars(config, {}, secrets.EnvSource(needed))
    assert "TF_VAR_bws_access_token" not in variables
    assert "bws_access_token" not in (REPO / "tofu" / "stacks" / "edge" / "variables.tf").read_text(encoding="utf-8")


# -- the workflow ------------------------------------------------------------------------
def workflow():
    return yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))


def test_the_reusable_workflow_declares_the_doppler_tokens_as_optional_secrets():
    declared = workflow()[True]["workflow_call"]["secrets"]
    for name in ("DOPPLER_TOKEN", "DOPPLER_TOKEN_APPS"):
        assert declared[name] == {"required": False}


def test_the_tokens_reach_the_engine_by_name_and_never_on_a_command_line():
    steps = workflow()["jobs"]["run"]["steps"]
    plain = next(s for s in steps if str(s.get("name", "")).startswith("sealedstack ") and "(secret source ci)" not in s["name"])
    for name in ("DOPPLER_TOKEN", "DOPPLER_TOKEN_APPS"):
        assert plain["env"][name] == "${{ secrets." + name + " }}"
        assert f"-e {name}" in plain["run"]
        assert "secrets." not in plain["run"]  # the value is never interpolated into the script


def test_the_tailscale_fallback_can_read_the_oauth_client_from_doppler():
    steps = workflow()["jobs"]["run"]["steps"]
    trust = next(s for s in steps if s.get("id") == "ts")
    assert 'elif [ -n "$BWS_ACCESS_TOKEN" ] || [ -n "$DOPPLER_TOKEN" ]' in trust["run"]
    fallback = next(s for s in steps if s.get("id") == "ts_secret")
    assert "-e DOPPLER_TOKEN -e DOPPLER_TOKEN_APPS" in fallback["run"]
    assert fallback["env"]["DOPPLER_TOKEN"] == "${{ secrets.DOPPLER_TOKEN }}"


def test_the_registry_entry_is_native_and_needs_no_cli():
    entry = next(e for e in yaml.safe_load((REPO / "registry" / "secrets.yaml").read_text())["implementations"] if e["id"] == "doppler")
    assert entry["impl"] == {"adapter": "engine/sealedstack/doppler.py"}
    assert (REPO / entry["impl"]["adapter"]).is_file()
    assert entry["secrets"] == ["DOPPLER_TOKEN"]


def test_the_engine_image_never_needed_a_doppler_cli():
    for dockerfile in ("Dockerfile", "toolbox/Dockerfile"):
        assert "doppler" not in (REPO / dockerfile).read_text(encoding="utf-8").lower()


# -- the docs cannot drift from the code ---------------------------------------------------
def test_the_naming_table_in_the_prerequisites_matches_the_code():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    block = text.split("### Doppler secret names", 1)[1].split("**Limits to know:**", 1)[0]
    rows = [line for line in block.splitlines() if line.startswith("| `")]
    assert len(rows) >= 3
    for row in rows:
        left, right = [cell.strip() for cell in row.strip("|").split("|")]
        if right == "the same":
            keys = [k.strip().strip("`") for k in left.split(",")]
            assert all(doppler.doppler_name(k) == k for k in keys), row
        else:
            assert doppler.doppler_name(left.strip("`")) == right.strip("`"), row


def test_the_docs_tell_users_which_secret_to_create_and_that_the_token_is_read_only():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    section = text.split("## Using Doppler instead of Bitwarden", 1)[1].split("## Server credentials", 1)[0]
    for needle in ("DOPPLER_TOKEN", "DOPPLER_TOKEN_APPS", "dp.st.", "write access off", "slots.secrets: doppler", "--secrets doppler"):
        assert needle in section, needle


def test_d066_is_recorded():
    text = (REPO / "docs" / "decisions.md").read_text(encoding="utf-8")
    assert "### D-066" in text and "Doppler is a native, read-only secret source" in text
