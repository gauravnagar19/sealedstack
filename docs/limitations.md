# Known limitations and caveats

sealedstack is **v0.5 (pre-release)** and has not yet been applied against live
accounts or a real server. This page is the candid list of things that will bite
you: what does not work, what only works with caveats, and the environmental
requirements behind both. For the capability matrix see status (private)
and the verification log (private); for the design rationale behind a
few of these, see [decisions.md](decisions.md).

- **Secret sources are read-only (D-038).** The engine only reads secrets; it never writes them. Tunnel tokens and other generated values live only in encrypted state. If you are upgrading from a build that wrote them to the source, `tofu state rm` the old `module.secrets_*` resources before the first apply (the release notes list the addresses).

- **R2 access uses the `aws` CLI.** Database backups/restores and the state backend read/write R2 through the bundled `aws` CLI (the engine signs S3 via it); unlike `gh`/`jq`/`yq`/`curl`, `aws` stays in the toolbox image.

## No config ships in this repository

The repository is the *tool*, not a deployment. The user-specific config files
are gitignored (D-022, review finding F4) and the repo carries only samples:

```
config/stack.example.yaml          config/servers.example.yaml
config/apps.example.yaml           config/providers/*.example.yaml
```

`config/stack.yaml`, `config/servers.yaml`, `config/apps.yaml` and
`config/tailscale-policy.hujson` are all in `.gitignore`. **`check`, `plan`
and `apply` all need a real `config/` to exist.** In your infra repo, the Setup
job creates it as a pull request; in a fresh checkout of the tool repo they fail
until you create one.

Contributors running the tool from a checkout can create one in three ways:

```sh
# 1. Interactive wizard (writes config/ locally; `setup` itself is the non-interactive CI job)
sealedstack debug init

# 2. Copy the samples and edit
cp config/stack.example.yaml   config/stack.yaml
cp config/servers.example.yaml config/servers.yaml
cp config/apps.example.yaml    config/apps.yaml

# 3. Point at a separate (private) config directory
export SEALEDSTACK_CONFIG=/absolute/path/to/your-config
```

`SEALEDSTACK_CONFIG` is the single source of truth for the config directory;
when it is unset, scripts fall back to `<repo>/config`. See
[config-repo.md](config-repo.md) and [getting-started.md](getting-started.md)
(the Setup step).

## OpenTofu version floor

- **Provider configurations with `for_each`** (how the compute stack dispatches
  per-provider) require OpenTofu **≥ 1.9**. The `edge` and `compute` stacks
  declare `required_version = ">= 1.9"`; `platform` and `state` declare
  `>= 1.8`.
- **S3-native state locking** (`use_lockfile`) requires **≥ 1.10**. Every
  `tofu_ready` call initialises the backend with `use_lockfile = true`, so the
  *effective* floor for all four stacks is **1.10** even where the module
  declares a lower one.
- The toolbox image pins **OpenTofu 1.13.1**, which satisfies all of the above.
  If you run the toolchain outside the toolbox, use 1.13.x.

Because the compute providers are now **aliased configurations**
(`alias = "this"` with `for_each`), an existing state file that was originally
created against a *default* (non-aliased) provider config may need a one-time
**provider re-association** on the first apply after the change. Running
`tofu init -reconfigure` (which `tofu_ready` already does) is the normal fix;
if a plan reports provider-config moves, follow the re-association prompt.

## Generated values live only in the encrypted state

sealedstack never writes to the secret source (D-038). The Dokploy to worker SSH
key pair, the CI SSH key pair, the Dokploy admin password and the Dokploy API key
(created through Dokploy's API on first boot) are kept in the `generated` stack's
encrypted state. If an earlier build stored `DOKPLOY_SSH_PRIVATE_KEY` /
`DOKPLOY_SSH_PUBLIC_KEY` in Bitwarden, they are simply no longer read; delete
them. The Dokploy first-boot calls are pinned to Dokploy v0.30.8 and have been
exercised against a mocked API only; if Dokploy changes them, create a key by hand
and add it as `DOKPLOY_API_KEY`.

## Secrets providers: which can be read back

The secrets slot is used for both writing and reading secrets. Not every backend
implements both directions. `engine/sealedstack/secrets.py` is the read path:

| Readable backends | How |
|---|---|
| `bitwarden` (default) | `bws secret list`, with a parent-shell prefetch cache |
| `sops` | `sops --decrypt config/secrets/<project>.enc.yaml` |
| `aws_ssm` | `aws ssm get-parameter --with-decryption` |
| `vault` / `openbao` | `vault kv get` (KV v2) |
| `infisical`, `doppler`, `onepassword`, `pass` | the provider's own CLI |

**`gcp` and `azure_kv` are write-only.** They have no read path in the toolbox
image, so `secrets_get` fails with a clear message (no CLI in the image; use the
provider console or export the value) instead of silently returning empty. If
you select one of them you must export those values as environment variables for
anything that reads secrets back.

The CLI adapters (`infisical`, `doppler`, `onepassword`, `pass`) require **their
CLI on `PATH`**; they are **not bundled in the toolbox image**, so they only
work where the CLI is installed. The same applies to `vault` against a real
Vault/OpenBao if the CLI is absent.

## Image retention in the release workflow is best-effort

The reusable app release workflow prunes old images after a release: it keeps
the **last 30 version tags plus the currently-deployed version**, and deletes
untagged images older than 7 days. This is deliberately `continue-on-error` and
best-effort. It may **silently skip** deletions when:

- `GITHUB_TOKEN` lacks permission to delete packages (common for user-owned
  packages), or
- the package is owned by an org or user whose API scope doesn't match, or
- no `SEALEDSTACK_RELEASED_VERSION` / Dokploy lookup is available to identify
  the currently-deployed version.

Retention never breaks a build; it just may not happen.

## Documentation links are checked by the tests

`mkdocs.yml` reports broken relative links and missing anchors as warnings, and the
docs build runs with `--strict`, so a broken cross-reference fails the build.
Absolute and non-page links (for example to files in the repository) are checked by
the test suite (`tests/pytest/test_docs.py`).

## The end-to-end test spends real money and touches real accounts

The end-to-end test (`python3 -m sealedstack.e2e`, a contributor tool) creates a real VM, bootstraps it, applies the
edge and platform stacks against a real Cloudflare/Tailscale/Bitwarden account,
then destroys everything. It is intentionally hard to run by accident:

- It refuses unless **`E2E_CONFIRM=yes`** is set.
- It requires a **separate throwaway config directory** (`config-e2e/`, or
  `SEALEDSTACK_E2E_CONFIG`) and a **separate state prefix**
  (`SEALEDSTACK_STATE_PREFIX=e2e`), so it cannot touch production config or the
  production state file.
- It must use a **throwaway domain/subdomain** (a real Cloudflare zone).

Even so, it **spends money** (the VM) and **changes real provider state**.
Read it before running it; never point it at your production config.

## The tool image owner defaults to a placeholder

`scripts/init.py` defaults the published image owner to `anomalyco`
(`SEALEDSTACK_IMAGE_OWNER`, or `DEFAULT_OWNER`), and the toolbox `Dockerfile`
defaults `TOOLBOX_OWNER=anomalyco`. If you build or publish your own images, set
the owner explicitly:

```sh
docker build --build-arg TOOLBOX_OWNER=<your-gh-owner> -f Dockerfile .
export SEALEDSTACK_IMAGE_OWNER=<your-gh-owner>
python3 scripts/init.py --owner <your-gh-owner>
```

Otherwise the generated `bin/sealedstack` and CI snippets point at an image
namespace that is not yours.

## Approvals on GitHub Free private repositories: what they do and don't protect

**Background.** On a **private** repository on the **GitHub Free** plan, GitHub doesn't
offer branch protection, rulesets, or environment protection rules (required
reviewers, deployment-branch limits). sealedstack therefore enforces its own gate
(D-052) for every write command (`apply`, `up`, `restore`, `add-server`,
`server sync`). A write runs only when **all** of these hold:

1. the run is on the default branch (`refs/heads/main`) **and** its commit is the
   current head of that branch;
2. the forge reports the commit as **merged through a pull request** (direct
   pushes are refused);
3. it is **approved**: dispatched by someone in `stack.yaml: approvers`, or an
   approver commented `/sealedstack approve` on that PR after the latest plan.

When your plan *does* support environment protection (public repos, or paid
plans), sealedstack uses it as well, as a second layer.

**What this protects against:** mistakes and unreviewed changes. A config edit can't
reach your servers until it is merged through a PR and approved by an approver.

**What it does *not* protect against: a malicious collaborator with write access.**
On Free private repos, CI secrets (such as `BWS_ACCESS_TOKEN`) are *repository*
secrets, readable by a workflow on **any** branch. Anyone who can push a branch can
edit a workflow on that branch and use the secrets directly, without going
through sealedstack at all. No check inside sealedstack can prevent that.

**Recommendations:**

- **Keep write access to your infra repo to people you'd trust with your servers.**
  A single-writer infra repo is the safe default.
- If several people need write access, use a plan that supports environments,
  and put the secrets in a `production` **environment** limited to the default
  branch with required reviewers. sealedstack's workflows already reference that
  environment.
- **Minimise what the CI secret can do:** give the Bitwarden machine account
  **read-only** access to the `infra` project only, and scope every provider
  token to the exact permissions in [prerequisites](prerequisites.md).
- **Rotate** the CI-held secrets when someone leaves the repo.

## Other open caveats

These are tracked in status (private) and verification log (private):

- **No provider is `supported` yet.** Every registry implementation is
  `experimental` (code complete, never run live) or `manual`/`planned` (D-027).
  Do not treat "validates offline" as "tested against the real provider".
- **Netcup is `manual`.** The SCP firewall adapter is not implemented (H5);
  selecting `provider: netcup` expects you to apply the firewall yourself.
- **Multi-server apps with a database are rejected** by validation until
  cross-server Postgres over Tailscale lands (D-029, H6). DB apps are
  single-server for now.
- **Private app repositories** need a Dokploy GitHub App or a deploy key
  (H7). The prebuilt-image model (D-024) removes the need to clone source on the
  server, but this is still listed as an open blocker for the first live run.
- **Nothing has run live.** The end-to-end apply, the backup/restore drill and
  the Ansible roles have not executed against a real server or account. See
  status (private).
