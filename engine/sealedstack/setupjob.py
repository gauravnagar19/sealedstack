"""``sealedstack setup``: the non-interactive Setup job (UX §4 steps 3-6).

    sealedstack setup --domain example.com --email you@example.com --admin admin \\
        --server server-1 --provider netcup --secrets bitwarden

It asks for nothing else. It writes ``config/`` (with the secret key names written
out, D-050), ``.devcontainer/`` and a README on the branch ``sealedstack/setup``,
opens the pull request "sealedstack setup" through the forge and posts the living
checklist comment. Nothing outside the repository changes.

``/sealedstack check`` on that PR (``prepare_check`` + ``checklist_items``) then:
fills in what the credentials reveal (Cloudflare account id and team name, the
MagicDNS suffix), imports the user's tailnet policy into
``config/tailscale-policy.hujson``, each as a commit on the setup branch, and
rewrites the checklist in place.

The interactive wizard remains only as ``sealedstack debug init``.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

import yaml

from . import botmanifest
from . import checklist
from . import config as config_mod
from . import discover
from . import forge as forge_mod
from . import preflight
from . import scaffold
from . import secrets as secrets_mod
from .http import HttpClient, UrllibClient

SETUP_BRANCH = "sealedstack/setup"
PR_TITLE = "sealedstack setup"
POLICY_FILE = "config/tailscale-policy.hujson"

POLICY_HEADER = """// Tailnet policy imported by `sealedstack check` from the Tailscale API (D-026).
// sealedstack merges only its own tags, rules and tests into this file at plan
// time; every rule below is preserved. Edit freely.
"""


class SetupError(RuntimeError):
    """A usage or precondition problem; the message says how to fix it."""


@dataclass
class SetupArgs:
    domain: str
    email: str
    admin: str = "admin"
    servers: list = field(default_factory=list)  # [(name, provider, role, os)]
    secrets: str = "bitwarden"
    name: str = ""
    owner: str = ""
    ssh_keys: list = field(default_factory=list)
    force: bool = False
    local: bool = False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sealedstack setup",
        description="Write config/ and open the Setup pull request with its living checklist.",
    )
    parser.add_argument("--domain", required=True, help="apex domain on Cloudflare, e.g. example.com")
    parser.add_argument("--email", required=True, help="your email (the Cloudflare Access login)")
    parser.add_argument("--admin", default="admin", help="Linux admin user created on every server (default: admin)")
    parser.add_argument("--server", action="append", required=True, metavar="NAME",
                        help="a server name; repeat for more (the first is the control server)")
    parser.add_argument("--provider", default="any-ssh",
                        help="provider of the server(s): any-ssh, netcup, hetzner, ... (default: any-ssh)")
    parser.add_argument("--secrets", default="bitwarden", help="secret source: bitwarden (default), env, ci, sops")
    parser.add_argument("--name", default="", help="infra name (default: the repository name)")
    parser.add_argument("--owner", default="", help="owner of the engine image and the approver (default: the repository owner)")
    parser.add_argument("--os", default="ubuntu-24.04", help="server OS (default: ubuntu-24.04)")
    parser.add_argument("--ssh-key", action="append", default=[], metavar="PUBKEY",
                        help="an admin SSH public key (default: none; the checklist asks for it)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing config/")
    parser.add_argument("--local", action="store_true", help="write the files here instead of opening a PR")
    return parser


def parse_args(argv: Sequence[str]) -> SetupArgs:
    ns = build_parser().parse_args(list(argv))
    servers = []
    for index, name in enumerate(ns.server):
        servers.append((name, ns.provider, "control" if index == 0 else "worker", ns.os))
    return SetupArgs(
        domain=ns.domain, email=ns.email, admin=ns.admin, servers=servers, secrets=ns.secrets,
        name=ns.name, owner=ns.owner, ssh_keys=list(ns.ssh_key), force=ns.force, local=ns.local,
    )


def engine_image(owner: str, version: str) -> str:
    """The pinned engine image for ``owner`` (D-046): never a placeholder owner."""
    return f"ghcr.io/{owner}/sealedstack:{version}"


def build_files(args: SetupArgs, *, repo: str, version: str, owner: str) -> dict:
    """Every file the Setup PR adds, path -> text."""
    name = args.name or repo.split("/")[-1] or "sealedstack-infra"
    stack = scaffold.stack_config(
        infra_name=name, domain=args.domain, owner_email=args.email, admin_user=args.admin,
        ssh_keys=args.ssh_keys, approvers=[owner] if owner else [], account_id=discover.PLACEHOLDER,
        tailnet="", secrets_source=args.secrets,
    )
    servers = scaffold.servers_config(args.servers)
    apps = scaffold.apps_config(args.servers[0][0], args.domain)
    image = engine_image(owner, version)

    def dump(doc: dict, header: str = "") -> str:
        return header + yaml.safe_dump(doc, sort_keys=False)

    return {
        "config/stack.yaml": dump(
            stack,
            "# sealedstack stack config. Secret key names are written out under `secrets.keys` (D-050):\n"
            "# rename any value to match an entry you already have. No secret values live here.\n",
        ),
        "config/servers.yaml": dump(servers),
        "config/apps.yaml": dump(apps),
        ".devcontainer/devcontainer.json": scaffold.devcontainer_json(image),
        ".gitignore": scaffold.gitignore(),
        "README.md": scaffold.setup_readme(name, image, version),
    }


def pr_body(args: SetupArgs, repo: str) -> str:
    servers = ", ".join(f"`{s[0]}` ({s[1]})" for s in args.servers)
    return (
        f"Proposes the configuration for **{args.domain}** on {servers}.\n\n"
        "The checklist below is updated by `/sealedstack check`. Create the prerequisites it lists, comment "
        "`/sealedstack check`, and merge this PR when every item is ✅. **Nothing outside this repository "
        "has been changed.**\n\n"
        "Review `config/stack.yaml` (secret key names under `secrets.keys`, `approvers`), `config/servers.yaml` "
        "(the key names of each server's bootstrap secrets) and `config/apps.yaml` (the first app, `hello`)."
    )


# --------------------------------------------------------------------------- #
# the Setup job
# --------------------------------------------------------------------------- #
def run(
    argv: Sequence[str],
    *,
    env: Optional[Mapping[str, str]] = None,
    forge: Optional[forge_mod.Forge] = None,
    root: Optional[Path] = None,
    http: Optional[HttpClient] = None,
    secrets: Optional[secrets_mod.SecretSource] = None,
    tcp_probe: Optional[Callable] = None,
    out=None,
) -> int:
    out = out if out is not None else sys.stdout
    env = dict(os.environ if env is None else env)
    root = Path(root) if root else Path.cwd()
    try:
        args = parse_args(argv)
    except SystemExit as exc:  # argparse already printed the usage
        return int(exc.code or 2)

    try:
        version = _read_version(root)
        repo = env.get("GITHUB_REPOSITORY", "")
        if not repo and not args.local:
            repo = forge_mod.repo_from_git(root)
        if not repo and not args.local:
            raise SetupError("could not tell which repository this is; run it from the infra repo's workflow "
                             "(GITHUB_REPOSITORY) or pass --local to just write the files")
        owner = args.owner or (repo.split("/")[0] if repo else "")
        if not owner:
            raise SetupError("could not tell the owner (the engine image namespace and the approver); pass --owner")
        if (root / "config" / "stack.yaml").exists() and not args.force:
            raise SetupError("config/stack.yaml already exists: this repository is already set up "
                             "(re-run with --force to replace it)")
        files = build_files(args, repo=repo, version=version, owner=owner)
    except SetupError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1

    if args.local:
        for path, text in files.items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            print(f"  write {path}", file=out)
        print("sealedstack: wrote the Setup files; commit them in a branch and open a PR.", file=out)
        return 0

    if forge is None:
        try:
            forge = forge_mod.GitHubForge.from_env(env)
        except forge_mod.ForgeError as exc:
            print(f"sealedstack: {exc} (needed to open the Setup PR)", file=sys.stderr)
            return 1

    base = env.get("SEALEDSTACK_DEFAULT_BRANCH") or "main"
    number = forge.find_open_pr(repo, SETUP_BRANCH)
    if number:
        forge.commit_files(repo, SETUP_BRANCH, files, f"sealedstack: update {PR_TITLE}")
        print(f"sealedstack: updated the open Setup PR #{number}", file=out)
    else:
        number = forge.open_pr(repo, branch=SETUP_BRANCH, title=PR_TITLE, body=pr_body(args, repo), files=files, base=base)
        print(f"sealedstack: opened the Setup PR #{number}", file=out)

    items = initial_items(files, root=root, env=env, repo=repo, owner=owner, version=version,
                          http=http, secrets=secrets, tcp_probe=tcp_probe)
    forge.upsert_comment(repo, number, checklist.render(items), marker=checklist.MARKER)
    print(f"sealedstack: posted the setup checklist on PR #{number}. Create the prerequisites, then comment "
          "`/sealedstack check`.", file=out)
    return 0


def _read_version(root: Path) -> str:
    path = root / "sealedstack.version"
    if not path.is_file():
        raise SetupError("sealedstack.version is missing: add it (and the workflow) first, see the getting-started guide")
    version = path.read_text(encoding="utf-8").strip()
    if not version:
        raise SetupError("sealedstack.version is empty")
    return version[1:] if version.startswith("v") else version


# --------------------------------------------------------------------------- #
# running the checks against the files (before and after they are merged)
# --------------------------------------------------------------------------- #
def initial_items(
    files: Mapping[str, str],
    *,
    root: Path,
    env: Mapping[str, str],
    repo: str,
    owner: str,
    version: str,
    http: Optional[HttpClient] = None,
    secrets: Optional[secrets_mod.SecretSource] = None,
    tcp_probe: Optional[Callable] = None,
) -> list:
    """The first checklist: run the preflight against a scratch copy of the new config."""
    with tempfile.TemporaryDirectory(prefix="sealedstack-setup-") as tmp:
        scratch = Path(tmp)
        for path, text in files.items():
            if path.startswith("config/") or path == "README.md":
                target = scratch / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        # the version pins live in the real checkout
        for rel in ("sealedstack.version", ".github/workflows/sealedstack.yml"):
            if (root / rel).is_file():
                target = scratch / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text((root / rel).read_text(encoding="utf-8"), encoding="utf-8")
        code_root = config_mod.find_code_root(env)
        config = config_mod.load_config(scratch / "config", code_root, env)
        ctx = preflight.build_context(
            config, env=dict(env), http=http, secrets=secrets, version=config_mod.read_version(code_root),
            tcp_probe=tcp_probe, build_dir=scratch / ".build",
        )
        results = preflight.run_checks(ctx)
    return checklist_items(results, config, repo=repo, owner=owner, version=version, env=env)


def prepare_check(
    config_dir: Path,
    *,
    env: Mapping[str, str],
    forge: forge_mod.Forge,
    repo: str,
    http: Optional[HttpClient] = None,
    secrets: Optional[secrets_mod.SecretSource] = None,
    out=None,
) -> list:
    """What ``/sealedstack check`` does on the Setup PR before it re-runs the checks.

    With the credentials in place it discovers the Cloudflare account id, team name
    and MagicDNS suffix and imports the tailnet policy, each as a commit on the
    setup branch (and into the working tree, so the checks that follow see them).
    Returns what it committed.
    """
    out = out if out is not None else sys.stderr
    http = http or UrllibClient()
    config_dir = Path(config_dir)
    config = config_mod.load_config(config_dir, config_mod.find_code_root(env), env)
    source = secrets or secrets_mod.build_source(config.stack, config.config_dir, dict(env))

    def secret(logical: str) -> str:
        result = secrets_mod.lookup(source, config, logical)
        return result.value if result.found else ""

    committed: list[str] = []
    root = config_dir.parent

    def commit(path: str, text: str, message: str) -> None:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        forge.commit_files(repo, SETUP_BRANCH, {path: text}, f"sealedstack: {message}")
        committed.append(message)
        print(f"sealedstack: committed to {SETUP_BRANCH}: {message}", file=out)

    stack_path = config_dir / "stack.yaml"
    stack_text = stack_path.read_text(encoding="utf-8")
    found: dict = {}

    cf_token = secret("cloudflare_api_token")
    if cf_token:
        try:
            account = str((config.stack.get("cloudflare") or {}).get("account_id") or "")
            if account in ("", discover.PLACEHOLDER):
                account = discover.cloudflare_account(http, cf_token)
                if account:
                    found["account_id"] = account
            if account and account != discover.PLACEHOLDER:
                team = discover.cloudflare_team(http, cf_token, account)
                if team:
                    found["team_name"] = team
        except Exception as exc:  # noqa: BLE001 - discovery is best effort; check explains the gaps
            print(f"sealedstack: could not read Cloudflare facts: {exc}", file=out)

    ts_id, ts_secret = secret("tailscale_oauth_client_id"), secret("tailscale_oauth_client_secret")
    ts_token = ""
    if ts_id and ts_secret:
        try:
            ts_token = discover.tailscale_token(http, ts_id, ts_secret)
            if ts_token and not (config.stack.get("mesh") or {}).get("tailnet"):
                suffix = discover.tailnet_suffix(http, ts_token)
                if suffix:
                    found["tailnet"] = suffix
        except Exception as exc:  # noqa: BLE001
            print(f"sealedstack: could not read Tailscale facts: {exc}", file=out)

    if found:
        new_text, changed = discover.fill_stack(stack_text, **found)
        if changed:
            commit("config/stack.yaml", new_text, "fill in " + ", ".join(changed) + " found with your credentials")

    policy_path = config_dir / "tailscale-policy.hujson"
    if ts_token and not policy_path.exists():
        try:
            policy = discover.tailscale_policy(http, ts_token)
        except Exception as exc:  # noqa: BLE001
            policy = ""
            print(f"sealedstack: could not read the Tailscale policy: {exc}", file=out)
        if policy:
            body = policy if policy.endswith("\n") else policy + "\n"
            commit(POLICY_FILE, POLICY_HEADER + body, "import your tailnet policy (config/tailscale-policy.hujson)")
    return committed


# --------------------------------------------------------------------------- #
# the checklist rows (UX §4 step 4)
# --------------------------------------------------------------------------- #
def docs_link(owner: str, version: str, anchor_path: str) -> str:
    return f"https://github.com/{owner}/sealedstack/blob/v{version}/docs/{anchor_path}"


def checklist_items(
    results: Sequence,
    config: config_mod.Config,
    *,
    repo: str,
    owner: str,
    version: str,
    env: Optional[Mapping[str, str]] = None,
) -> list:
    """Turn preflight results into the prerequisite rows of the Setup checklist."""
    env = env or {}
    by_id = {r.id: r for r in results}
    items: list[checklist.Item] = []
    sk = config.secret_key
    source_label = (config.stack.get("slots") or {}).get("secrets") or "bitwarden"
    project = config.source_project()

    def link(text: str, anchor: str) -> str:
        return f"[{text}]({docs_link(owner, version, anchor)})"

    def add(status: str, label: str, detail: str = "", fix: str = "") -> None:
        items.append(checklist.Item(status, label, detail, fix))

    # 1. config files
    cfg_results = [r for r in results if r.id.startswith("config.") and r.id != "config.compose"]
    bad = [r for r in cfg_results if r.status == "fail"]
    if bad:
        add("fail", "Config files", "; ".join(f"{r.id}: {r.detail}" for r in bad[:3]),
            "fix the file named above in this PR and comment `/sealedstack check`")
    else:
        add("ok", "Config files", "valid")

    # 2. version pins
    pin = by_id.get("versions.pin")
    if pin is not None:
        add("ok" if pin.status == "ok" else "fail",
            f"Version pins match (sealedstack.version = workflow ref = {version})" if pin.status == "ok"
            else "Version pins match", "" if pin.status == "ok" else pin.detail, pin.fix)

    # 3. the domain
    zone = by_id.get("cloudflare.zone")
    domain = config.domain
    if zone is None:
        add("pend", f"Domain `{domain}` is on Cloudflare", "waits for the Cloudflare API token")
    else:
        add("ok" if zone.status == "ok" else "fail", f"Domain `{domain}` is on Cloudflare",
            "" if zone.status == "ok" else zone.detail, "" if zone.status == "ok" else zone.fix)

    # 4. the secret source
    src = by_id.get("secrets.source")
    if src is not None:
        label = f"Secret source: {source_label.capitalize()}"
        if src.status == "ok":
            add("ok", label, src.detail)
        else:
            add("fail", label, src.detail,
                f"{src.fix} {link('How to create it (2 min)', 'prerequisites.md#what-you-create-and-where-it-goes')}")

    # 5. the Cloudflare token
    cf = by_id.get("secrets.cloudflare_api_token")
    perms = [r for r in results if r.id.startswith("cloudflare.perm.") and r.status == "fail"]
    cf_label = f"{sk('cloudflare_api_token')} (in {source_label.capitalize()}, project `{project}`)"
    if cf is not None and cf.status in ("ok", "warn") and not perms:
        r2 = by_id.get("cloudflare.r2_derived")
        if r2 is not None and r2.status == "fail":
            add("fail", cf_label, r2.detail, r2.fix)
        else:
            add("ok", cf_label, "present and verified" if by_id.get("cloudflare.token") else "present")
    elif cf is not None and cf.status in ("ok", "warn") and perms:
        add("fail", cf_label, "; ".join(r.detail for r in perms),
            link("Edit the token and add exactly these permissions", "prerequisites.md#cloudflare-api-token-permissions"))
    else:
        add("fail", cf_label, "not found",
            link("Create the token with exactly these permissions", "prerequisites.md#cloudflare-api-token-permissions")
            + ": Account · Cloudflare Tunnel: Edit · Access: Apps and Policies: Edit · Workers R2 Storage: Edit · "
              "Zone · DNS: Edit · Zone WAF: Edit · Bot Management: Edit · Single Redirect: Edit")

    # 6. the Tailscale OAuth client
    cid, csec = by_id.get("secrets.tailscale_oauth_client_id"), by_id.get("secrets.tailscale_oauth_client_secret")
    oauth = by_id.get("tailscale.oauth")
    ts_label = f"Tailscale OAuth client (`{sk('tailscale_oauth_client_id')}` / `_SECRET`)"
    present = all(r is not None and r.status in ("ok", "warn") for r in (cid, csec))
    if present and oauth is not None and oauth.status == "ok":
        add("ok", ts_label, "present and verified")
    elif present and oauth is not None and oauth.status == "fail":
        add("fail", ts_label, oauth.detail,
            link("Recreate the client: scopes `policy_file`, `auth_keys`, `devices:core`", "prerequisites.md#tailscale-oauth-client"))
    elif present:
        add("pend", ts_label, "present; waits for the API check")
    else:
        add("fail", ts_label, "not found",
            link("Create the client: scopes `policy_file`, `auth_keys`, `devices:core`", "prerequisites.md#tailscale-oauth-client"))

    # 7. the tailnet policy
    policy = by_id.get("tailscale.policy")
    allow = by_id.get("tailscale.allow_all")
    if policy is not None and policy.status == "ok" and "unmanaged" not in policy.detail:
        add("ok", "Tailscale policy imported into `config/tailscale-policy.hujson`", "")
    elif policy is not None and "unmanaged" in policy.detail:
        add("ok", "Tailscale policy", "unmanaged: you paste sealedstack's block by hand")
    else:
        add("pend", "Tailscale policy imported into `config/tailscale-policy.hujson`", "waits for the OAuth client (it also finds `mesh.tailnet`)")
    if allow is not None and allow.status == "fail":
        preview = f"https://github.com/{repo}/blob/{SETUP_BRANCH}/{POLICY_FILE}" if repo else ""
        add("fail", "Tailscale policy is allow-all",
            "Your rule `* → *` lets every device reach every server port, so servers would not be sealed.",
            "Set `mesh.narrow_allow_all: true` in `config/stack.yaml` (this PR) to rewrite it to "
            "`autogroup:member → autogroup:member:*`. Your devices keep reaching each other; servers are "
            "reachable only through sealedstack's rules." + (f" [Preview of the change]({preview})" if preview else ""))

    # 8. the state passphrase
    sp = by_id.get("secrets.state_passphrase")
    sp_label = f"{sk('state_passphrase')} (≥ 16 chars)"
    if sp is not None and sp.status in ("ok", "warn"):
        add("ok" if sp.status == "ok" else "fail", sp_label, "present" if sp.status == "ok" else sp.detail, sp.fix)
    else:
        add("fail", sp_label, "not found", f"generate one (for example `openssl rand -base64 24`) and add it to {source_label.capitalize()}")

    # 9. the servers
    for name in sorted((config.servers.get("servers") or {})):
        res = by_id.get(f"servers.{name}")
        spec = (config.servers.get("servers") or {}).get(name) or {}
        ip_key, pw_key = config.bootstrap_secrets(name)
        label = f"Server {name}: `ip_secret` → `{ip_key}`, `password_secret` → `{pw_key}` (from `servers.yaml`) + port 22 open"
        if res is None:
            add("pend", label, "waits for the other checks")
        elif res.status == "ok":
            add("ok", label, res.detail)
        elif res.status == "skip":
            add("pend", label, res.detail, res.fix)
        else:
            fix = res.fix
            if spec.get("provider") == "netcup":
                fix += " " + link("Netcup: open TCP 22 temporarily", "netcup-firewall.md")
            add("fail", label, res.detail, fix.strip())

    # 10. the bot
    bot = by_id.get("forge.bot")
    if bot is not None and bot.status == "ok":
        add("ok", "Bot identity", bot.detail)
    else:
        url = botmanifest.registration_url(botmanifest.BOT_NAME, f"https://github.com/{repo}" if repo else "")
        add("fail", "Bot identity (GitHub App)", "not created",
            f"**[Create \"sealedstack-bot\" (1 click)]({url})**, generate a private key, install it on this repo, and add "
            "`SEALEDSTACK_BOT_APP_ID`, `SEALEDSTACK_BOT_PRIVATE_KEY` and `SEALEDSTACK_BOT_INSTALLATION_ID` to the secret "
            "source (or add a fine-grained token as the repository secret `GH_TOKEN`)")

    # Anything else that is not green gets its own row: the checklist never reports
    # "ready" while the check itself would fail.
    covered = {
        "versions.pin", "cloudflare.zone", "cloudflare.token", "cloudflare.r2_derived", "cloudflare.zero_trust",
        "secrets.source", "secrets.cloudflare_api_token", "secrets.tailscale_oauth_client_id",
        "secrets.tailscale_oauth_client_secret", "secrets.state_passphrase", "tailscale.oauth", "tailscale.policy",
        "tailscale.allow_all", "forge.bot",
    }
    if not present:  # the suffix is found together with the OAuth client
        covered.add("tailscale.tailnet")
    covered |= {f"servers.{name}" for name in (config.servers.get("servers") or {})}
    for result in results:
        if result.id in covered or result.id.startswith(("config.", "cloudflare.perm.")):
            continue
        if result.status in ("fail", "warn"):
            label = result.id
            if result.id.startswith("secrets."):
                label = f"{secrets_mod.describe(config, result.id.split('.', 1)[1])} (in {source_label.capitalize()}, project `{project}`)"
            add("fail" if result.status == "fail" else "warn", label, result.detail, result.fix)

    # 11. optional keys
    uses_clerk = any(((a or {}).get("user_auth") == "clerk") for a in (config.apps.get("apps") or {}).values()) or (
        (config.stack.get("slots") or {}).get("user_auth") == "clerk")
    if not uses_clerk:
        add("skip", "Optional: Clerk keys", "not needed yet (no app uses Clerk)")
    return items


def main(argv: Optional[Sequence[str]] = None) -> int:
    return run(list(argv if argv is not None else sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
