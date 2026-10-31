"""Task 6 (m7-complete): `sealedstack setup` opens the Setup PR with a living checklist;
`/sealedstack check` fills it in, imports the policy and re-renders (in-memory forge)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import botmanifest, checklist, cli, config as cfg, discover, forge, infraci, preflight, scaffold, setupjob  # noqa: E402
from sealedstack.http import Route, StubClient  # noqa: E402
from sealedstack.secrets import BitwardenSource, EnvSource  # noqa: E402

SLUG = "you/my-infra"
ARGS = ["--domain", "example.com", "--email", "you@example.com", "--admin", "admin",
        "--server", "server-1", "--provider", "netcup", "--secrets", "bitwarden"]


def make_checkout(tmp_path, version="0.6.0"):
    root = tmp_path / "checkout"
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / "sealedstack.version").write_text(f"{version}\n")
    (root / ".github" / "workflows" / "sealedstack.yml").write_text(
        f"jobs:\n  sealedstack:\n    uses: you/sealedstack/.github/workflows/infra.yml@v{version}\n"
    )
    return root


def materialize(f, number, root):
    """Write the PR branch's files into the checkout (what actions/checkout of the PR gives the job)."""
    for (repo, ref, path), text in f.files.items():
        if repo == SLUG and ref == setupjob.SETUP_BRANCH:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")


def new_forge():
    f = forge.InMemoryForge()
    f.permissions[(SLUG, "you")] = "admin"
    return f


ENV = {"GITHUB_REPOSITORY": SLUG, "SEALEDSTACK_DEFAULT_BRANCH": "main"}


def run_setup(tmp_path, f, *, argv=ARGS, env=None, secrets=None):
    root = tmp_path / "checkout" if (tmp_path / "checkout").exists() else make_checkout(tmp_path)
    out = []
    rc = setupjob.run(
        argv, env={**ENV, **(env or {})}, forge=f, root=root, http=StubClient(),
        secrets=secrets or BitwardenSource({}, runner=lambda a: (1, "", "")),
        tcp_probe=lambda *a: False, out=type("O", (), {"write": lambda self, s: out.append(s), "flush": lambda self: None})(),
    )
    return rc, root


# ---------------------------------------------------------------- the flags and the files
def test_setup_takes_exactly_the_documented_flags_and_nothing_interactive():
    args = setupjob.parse_args(ARGS)
    assert (args.domain, args.email, args.admin, args.secrets) == ("example.com", "you@example.com", "admin", "bitwarden")
    assert args.servers == [("server-1", "netcup", "control", "ubuntu-24.04")]
    two = setupjob.parse_args([*ARGS, "--server", "server-2"])
    assert [s[2] for s in two.servers] == ["control", "worker"]


def test_cli_setup_is_the_job_and_the_wizard_is_debug_init():
    seen = []
    cli.run(["setup", *ARGS], code_root=REPO, env={}, runner=lambda argv, root: seen.append(list(argv)) or 0)
    assert seen[-1][1:3] == ["-m", "sealedstack.setupjob"] and "--domain" in seen[-1]
    cli.run(["debug", "init", "--infra-name", "x"], code_root=REPO, env={}, runner=lambda argv, root: seen.append(list(argv)) or 0)
    assert seen[-1][1].endswith("init.py")


def test_the_files_have_the_explicit_defaults_and_neutral_content(tmp_path):
    args = setupjob.parse_args(ARGS)
    files = setupjob.build_files(args, repo=SLUG, version="0.6.0", owner="you")
    assert set(files) == {"config/stack.yaml", "config/servers.yaml", "config/apps.yaml", ".devcontainer/devcontainer.json",
                          ".gitignore", "README.md"}
    stack = yaml.safe_load(files["config/stack.yaml"])
    assert stack["name"] == "my-infra" and stack["approvers"] == ["you"] and stack["admin"]["ssh_keys"] == []
    assert stack["secrets"]["keys"]["state_passphrase"] == "STATE_PASSPHRASE"
    assert stack["cloudflare"]["account_id"] == "REPLACE_ME" and stack["mesh"]["tailnet"] == ""
    servers = yaml.safe_load(files["config/servers.yaml"])
    assert servers["servers"]["server-1"]["bootstrap"]["ip_secret"] == "SERVER_IP_SERVER_1"
    assert "hello" in yaml.safe_load(files["config/apps.yaml"])["apps"]
    # the engine image is pinned and owned by the repository owner, never a placeholder
    dev = json.loads(files[".devcontainer/devcontainer.json"])
    assert dev["image"] == "ghcr.io/you/sealedstack:0.6.0"
    blob = "\n".join(files.values())
    assert "anomalyco" not in blob and not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", blob)
    # all of it validates against the schemas
    sys.path.insert(0, str(REPO / "scripts"))
    import validate

    for name, doc in (("stack", stack), ("servers", servers)):
        assert validate.schema_errors(doc, REPO / "schemas" / f"{name}.schema.json") == []


# ---------------------------------------------------------------- the PR and the first checklist
def test_setup_opens_the_pr_and_posts_the_living_checklist(tmp_path):
    f = new_forge()
    rc, root = run_setup(tmp_path, f)
    assert rc == 0
    assert len(f.prs) == 1
    pr = f.prs[0]
    assert pr["branch"] == "sealedstack/setup" and pr["title"] == "sealedstack setup" and pr["base"] == "main"
    assert "config/stack.yaml" in pr["files"] and ".devcontainer/devcontainer.json" in pr["files"]
    comment = f.comments[(SLUG, pr["number"])][0].body
    assert checklist.MARKER in comment
    assert "Config files" in comment and "valid" in comment
    assert "Version pins match (sealedstack.version = workflow ref = 0.6.0)" in comment
    assert "BWS_ACCESS_TOKEN" in comment                       # the secret source row explains itself
    assert "CLOUDFLARE_API_TOKEN (in Bitwarden, project `infra`)" in comment
    assert "Workers R2 Storage: Edit" in comment and "Single Redirect: Edit" in comment
    assert "`TAILSCALE_OAUTH_CLIENT_ID` / `_SECRET`" in comment and "policy_file" in comment
    assert "waits for the OAuth client" in comment
    assert "STATE_PASSPHRASE (≥ 16 chars)" in comment
    assert "`SERVER_IP_SERVER_1`" in comment and "`ROOT_PASSWORD_SERVER_1`" in comment and "port 22" in comment
    assert "Netcup: open TCP 22 temporarily" not in comment or "docs/netcup-firewall.md" in comment
    # D-058: the bot identity is a fine-grained token, not an App
    assert "fine-grained token `GH_TOKEN`" in comment and "github.com/settings/personal-access-tokens/new?" in comment
    assert "Contents: read and write" in comment and "Pull requests: read and write" in comment
    assert "Variables: read and write" in comment and "Secrets: read and write" in comment
    assert "github.com/settings/apps/new" not in comment and "GitHub App" not in comment
    assert "Optional: Clerk keys" in comment and "not needed yet" in comment
    ready, total = map(int, re.search(r"Progress: (\d+) of (\d+) ready", comment).groups())
    assert 0 < ready < total and ready >= 2  # config + version pins are already green
    assert "Merge this PR when everything above is ✅" in comment


def test_the_app_link_is_hidden_behind_experimental_app(tmp_path):
    f = new_forge()
    rc, _ = run_setup(tmp_path, f, argv=[*ARGS, "--experimental-app"])
    assert rc == 0
    comment = f.comments[(SLUG, f.prs[0]["number"])][0].body
    assert "github.com/settings/apps/new?" in comment and "not used yet (D-058)" in comment
    assert "fine-grained token `GH_TOKEN`" in comment  # the token stays the default


def test_the_token_link_prefills_the_fine_grained_form():
    from sealedstack import botpermissions

    url = botpermissions.token_url("you/my-infra")
    assert url.startswith("https://github.com/settings/personal-access-tokens/new?")
    assert "name=sealedstack-bot" in url and "target_name=you" in url and "expires_in=90" in url
    assert "contents=write" in url and "pull_requests=write" in url and "issues=write" in url
    assert "metadata=read" in url and "administration" not in url  # optional permission is not pre-filled


def test_the_bot_link_prefills_the_app_registration():
    url = botmanifest.registration_url("sealedstack-bot", "https://github.com/you/my-infra")
    assert url.startswith("https://github.com/settings/apps/new?")
    assert "name=sealedstack-bot" in url and "pull_requests=write" in url and "issues=write" in url
    assert "contents=write" in url and "public=false" in url and "metadata" not in url


def test_a_second_setup_updates_the_same_pr(tmp_path):
    f = new_forge()
    run_setup(tmp_path, f)
    rc, _ = run_setup(tmp_path, f, argv=[*ARGS[:-2], "--secrets", "bitwarden"])
    assert rc == 0 and len(f.prs) == 1
    assert f.commits and f.commits[0]["branch"] == "sealedstack/setup"
    assert len(f.comments[(SLUG, f.prs[0]["number"])]) == 1  # the checklist is upserted


def test_setup_refuses_when_already_set_up_and_explains(tmp_path, capsys):
    f = new_forge()
    root = make_checkout(tmp_path)
    (root / "config").mkdir()
    (root / "config" / "stack.yaml").write_text("name: x\n")
    rc, _ = run_setup(tmp_path, f)
    assert rc == 1 and not f.prs
    assert "already set up" in capsys.readouterr().err


def test_setup_needs_the_version_pin_first(tmp_path, capsys):
    rc = setupjob.run(ARGS, env=ENV, forge=new_forge(), root=tmp_path / "empty")
    assert rc == 1 and "sealedstack.version is missing" in capsys.readouterr().err


def test_local_mode_writes_files_without_a_forge(tmp_path):
    root = make_checkout(tmp_path)
    rc = setupjob.run([*ARGS, "--local", "--owner", "you", "--name", "my-infra"], env={}, root=root)
    assert rc == 0 and (root / "config" / "stack.yaml").exists() and (root / ".devcontainer" / "devcontainer.json").exists()


# ---------------------------------------------------------------- check: discovery, policy commit, rows
CF_ROUTES = [
    ("tokens/verify", Route(json_body={"success": True, "result": {"id": "example-token-id"}})),
    ("access/organizations", Route(json_body={"success": True, "result": {"auth_domain": "teamx.cloudflareaccess.com"}})),
    ("/accounts?per_page", Route(json_body={"success": True, "result": [{"id": "acct999"}]})),
    ("zones?name=example.com", Route(json_body={"success": True, "result": [{"id": "zone123"}]})),
    ("/dns_records", Route(json_body={"success": True})),
    ("/cfd_tunnel", Route(json_body={"success": True})),
    ("/access/apps", Route(json_body={"success": True})),
    ("/r2/buckets", Route(json_body={"success": True})),
    ("rulesets?per_page=1", Route(json_body={"success": True})),
    ("/bot_management", Route(json_body={"success": True})),
    ("http_request_dynamic_redirect", Route(json_body={"success": True})),
]
POLICY = '{\n  // my tailnet\n  "acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:member:*"]}],\n}\n'
TS_ROUTES = [
    ("oauth/token", Route(json_body={"access_token": "at"})),
    ("/tailnet/-/acl", Route(text=POLICY)),
    ("/devices", Route(json_body={"devices": [{"name": "laptop.tail1234.ts.net"}]})),
]


def stub(*, cloudflare=True, tailscale=True):
    http = StubClient()
    for needle, route in (CF_ROUTES if cloudflare else []) + (TS_ROUTES if tailscale else []):
        http.add(needle, route)
    return http


SECRETS_ALL = {
    "CLOUDFLARE_API_TOKEN": "cf-token", "TAILSCALE_OAUTH_CLIENT_ID": "ts-id", "TAILSCALE_OAUTH_CLIENT_SECRET": "ts-secret",
    "STATE_PASSPHRASE": "0123456789abcdef", "SERVER_IP_SERVER_1": "203.0.113.9", "ROOT_PASSWORD_SERVER_1": "pw",
    "BWS_ACCESS_TOKEN": "bws",
}


def check_runner(config_dir, env, http, secrets, probe):
    def run(argv):
        config = cfg.load_config(config_dir, REPO, env)
        ctx = preflight.build_context(config, env=dict(env), http=http, secrets=secrets, version="0.6.0",
                                      tcp_probe=probe, build_dir=Path(config_dir).parent / ".build")
        results = preflight.run_checks(ctx)
        return (1 if any(r.status == "fail" for r in results) else 0), preflight.render_json(results, version="0.6.0")

    return run


def check_event(number):
    return infraci.Event(name="issue_comment", actor="you", repo=SLUG, ref="refs/heads/main", sha="a" * 40, default_branch="main")


def test_the_full_journey_setup_then_credentials_then_check_then_merge(tmp_path):
    f = new_forge()
    rc, root = run_setup(tmp_path, f)
    assert rc == 0
    number = f.prs[0]["number"]
    materialize(f, number, root)
    config_dir = root / "config"
    event = check_event(number)
    res = infraci.Resolution(command="check", pr=number)
    probe = lambda host, port, timeout: host == "203.0.113.9"  # only the bootstrap IP answers (port 22 open)

    # 1) nothing added yet: rows are red / pending
    config = cfg.load_config(config_dir, REPO, {})
    env1 = {"SEALEDSTACK_CONFIG": str(config_dir)}
    nothing = BitwardenSource({}, runner=lambda a: (1, "", ""))
    assert infraci.execute(res, event, f, config, check_runner(config_dir, env1, stub(cloudflare=False, tailscale=False), nothing, probe),
                           env=env1, http=stub(cloudflare=False, tailscale=False), secrets=nothing) == 1
    first = f.comments[(SLUG, number)][0].body
    assert "| ❌ |" in first and "| ⏳ |" in first
    ready, total = map(int, re.search(r"Progress: (\d+) of (\d+) ready", first).groups())
    assert ready < total

    # 2) the user adds the credentials and pins the host key from the provider console
    # (the checklist row says where), then comments /sealedstack check
    servers_file = config_dir / "servers.yaml"
    servers_doc = yaml.safe_load(servers_file.read_text())
    servers_doc["servers"]["server-1"]["bootstrap"] = {"host_key_fingerprint": "SHA256:" + "A" * 43}
    servers_file.write_text(yaml.safe_dump(servers_doc))
    config = cfg.load_config(config_dir, REPO, {})
    source = EnvSource(SECRETS_ALL)
    env2 = {**env1, **SECRETS_ALL, "SEALEDSTACK_BOT_TOKEN_SET": "true"}
    http = stub()
    config = cfg.load_config(config_dir, REPO, {})
    rc = infraci.execute(res, event, f, config, check_runner(config_dir, env2, http, source, probe), env=env2, http=http, secrets=source)
    comments = f.comments[(SLUG, number)]
    assert len(comments) == 1, "the checklist is updated in place, not duplicated"
    body = comments[0].body
    ready, total = map(int, re.search(r"Progress: (\d+) of (\d+) ready", body).groups())
    assert ready == total, body
    assert "| ❌ |" not in body and "| ⏳ |" not in body
    failing = [r for r in json.loads(check_runner(config_dir, env2, http, source, probe)([])[1])["results"] if r["status"] == "fail"]
    assert failing == [] and rc == 0

    # ... and the discovered facts + the policy are commits on the setup branch
    messages = [c["message"] for c in f.commits]
    assert any("fill in cloudflare.account_id" in m and "mesh.tailnet" in m for m in messages)
    assert any("import your tailnet policy" in m for m in messages)
    stack_text = f.files[(SLUG, setupjob.SETUP_BRANCH, "config/stack.yaml")]
    stack = yaml.safe_load(stack_text)
    assert stack["cloudflare"]["account_id"] == "acct999"
    assert stack["cloudflare"]["team_name"] == "teamx"
    assert stack["mesh"]["tailnet"] == "tail1234.ts.net"
    policy = f.files[(SLUG, setupjob.SETUP_BRANCH, "config/tailscale-policy.hujson")]
    assert policy.startswith("// Tailnet policy imported") and "my tailnet" in policy  # imported verbatim, comments kept
    assert (config_dir / "tailscale-policy.hujson").exists()  # in the working tree too

    # 3) merging: the committed config is what the engine will run against
    f.pull_requests[(SLUG, number)]["merged"] = True
    merged = cfg.load_config(config_dir, REPO, {})
    ctx = preflight.build_context(merged, env=env2, http=http, secrets=source, version="0.6.0", tcp_probe=probe,
                                  build_dir=root / ".build")
    assert all(r.status != "fail" for r in preflight.check_config(ctx))


def test_check_is_idempotent_once_everything_is_filled_in(tmp_path):
    f = new_forge()
    rc, root = run_setup(tmp_path, f)
    number = f.prs[0]["number"]
    materialize(f, number, root)
    config_dir = root / "config"
    source = EnvSource(SECRETS_ALL)
    env = {"SEALEDSTACK_CONFIG": str(config_dir), **SECRETS_ALL, "SEALEDSTACK_BOT_TOKEN_SET": "true"}
    http = stub()
    res = infraci.Resolution(command="check", pr=number)
    for _ in range(2):
        config = cfg.load_config(config_dir, REPO, {})
        infraci.execute(res, check_event(number), f, config, check_runner(config_dir, env, http, source,
                        lambda h, p, t: h == "203.0.113.9"), env=env, http=http, secrets=source)
    commits_after_two_runs = len(f.commits)
    assert commits_after_two_runs == 2  # stack.yaml fill-in + policy, once


def test_the_allow_all_row_and_its_fix_match_the_ux(tmp_path):
    f = new_forge()
    rc, root = run_setup(tmp_path, f)
    number = f.prs[0]["number"]
    materialize(f, number, root)
    config_dir = root / "config"
    (config_dir / "tailscale-policy.hujson").write_text('{"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}\n')
    f.files[(SLUG, setupjob.SETUP_BRANCH, "config/tailscale-policy.hujson")] = "x"
    env = {"SEALEDSTACK_CONFIG": str(config_dir), **SECRETS_ALL}
    source, http = EnvSource(SECRETS_ALL), stub()
    config = cfg.load_config(config_dir, REPO, {})
    infraci.execute(infraci.Resolution(command="check", pr=number), check_event(number), f, config,
                    check_runner(config_dir, env, http, source, lambda *a: False), env=env, http=http, secrets=source)
    body = f.comments[(SLUG, number)][0].body
    assert "Tailscale policy is allow-all" in body
    assert "lets every device reach every server port" in body
    assert "`mesh.narrow_allow_all: true`" in body and "autogroup:member → autogroup:member:*" in body
    assert "Preview of the change" in body


def test_a_non_setup_pr_check_still_uses_the_generic_checklist(tmp_path):
    f = new_forge()
    f.open_pr(SLUG, branch="feature/x", title="t", body="b", files={})
    number = f.prs[0]["number"]
    config = cfg.Config(REPO, tmp_path, {"name": "x"}, {}, {})
    out = '{"results": [{"id": "config.schema", "status": "ok", "detail": "valid"}]}'
    infraci.execute(infraci.Resolution(command="check", pr=number), check_event(number), f, config, lambda argv: (0, out), env={})
    assert "config.schema" in f.comments[(SLUG, number)][0].body
    assert not f.commits


# ---------------------------------------------------------------- the pieces
def test_fill_stack_only_fills_placeholders_and_keeps_comments():
    text = "cloudflare:\n  account_id: REPLACE_ME   # from the dashboard\n  team_name: example\nmesh:\n  tailnet: ''\n"
    new, changed = discover.fill_stack(text, account_id="acct1", team_name="teamx", tailnet="tail1.ts.net")
    assert "account_id: acct1" in new and "team_name: teamx" in new and "tailnet: tail1.ts.net" in new
    assert set(changed) == {"cloudflare.account_id", "cloudflare.team_name", "mesh.tailnet"}
    again, changed2 = discover.fill_stack(new, account_id="other", tailnet="other.ts.net")
    assert again == new and changed2 == []  # never overwrites what is already set


def test_cloudflare_account_is_only_taken_when_unambiguous():
    http = StubClient().add("/accounts", Route(json_body={"result": [{"id": "a"}, {"id": "b"}]}))
    assert discover.cloudflare_account(http, "t") == ""


def test_the_docs_links_in_the_checklist_resolve_to_real_headings():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    heads = {re.sub(r"[^a-z0-9 -]", "", h.lower()).strip().replace(" ", "-") for h in re.findall(r"^#+ (.+)$", text, re.M)}
    for anchor in ("cloudflare-api-token-permissions", "tailscale-oauth-client", "what-you-create-and-where-it-goes"):
        assert anchor in heads, anchor
    assert (REPO / "docs" / "netcup-firewall.md").exists()


# ---------------------------------------------------------------- the tag:release credential row (D-057)
def _row(comment, label_fragment):
    return next(line for line in comment.splitlines() if label_fragment in line)


def test_setup_checklist_has_the_manual_tag_release_credential_row_with_the_console_fields(tmp_path):
    f = new_forge()
    rc, _ = run_setup(tmp_path, f)
    assert rc == 0
    comment = f.comments[(SLUG, f.prs[0]["number"])][0].body
    row = _row(comment, "Tailscale OIDC trust credential for app releases (`tag:release`)")
    assert row.startswith("| — |")  # optional until an app has its own repository: not counted as a blocker
    for field in (
        "Settings → Trust credentials → Credential → **OpenID Connect**",
        "Issuer **GitHub**",
        "Subject `repo:you/<app>:environment:production`",
        "Scope **auth_keys: write**",
        "Tag **`tag:release`**",
        "`providers.mesh_tailscale.release_oauth_client_id`",
        "`release_audience`",
        "second credential",
    ):
        assert field in row, field
    ready, total = map(int, re.search(r"Progress: (\d+) of (\d+) ready", comment).groups())
    assert total == len([ln for ln in comment.splitlines() if ln.startswith("| ") and "Prerequisite" not in ln and not ln.startswith("| —")])


def _checklist(tmp_path, results, *, apps=None, stack_extra=None):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    stack = {"name": "my-infra", "domain": "example.com", "slots": {"mesh": "tailscale", "secrets": "env"}, **(stack_extra or {})}
    (config_dir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (config_dir / "servers.yaml").write_text("servers: {}\n")
    (config_dir / "apps.yaml").write_text(yaml.safe_dump(apps or {"apps": {}}))
    config = cfg.load_config(config_dir, REPO, {})
    items = setupjob.checklist_items(results, config, repo=SLUG, owner="you", version="0.6.0")
    return [i for i in items if "tag:release" in i.label]


def test_tag_release_row_turns_green_when_the_credential_is_in_stack_yaml(tmp_path):
    result = preflight.Result("tailscale.release_identity", "tailscale", "ok", detail="configured")
    (item,) = _checklist(tmp_path, [result])
    assert item.status == "ok" and "set in `stack.yaml`" in item.detail


def test_tag_release_row_is_red_with_the_steps_when_an_app_needs_it(tmp_path):
    result = preflight.Result("tailscale.release_identity", "tailscale", "fail", detail="release_audience not set, but 1 app(s) have a release workflow (shop)")
    (item,) = _checklist(tmp_path, [result])
    assert item.status == "fail" and "shop" in item.detail
    assert "Issuer **GitHub**" in item.fix and "Tag **`tag:release`**" in item.fix and "docs/ci-cd.md#2-tailscale-for-ci" in item.fix


def test_tag_release_row_is_not_duplicated_by_the_generic_failure_rows(tmp_path):
    result = preflight.Result("tailscale.release_identity", "tailscale", "fail", detail="missing")
    config_items = _checklist(tmp_path, [result])
    assert len(config_items) == 1


def test_no_tag_release_row_when_the_mesh_is_not_checked(tmp_path):
    assert _checklist(tmp_path, []) == []
