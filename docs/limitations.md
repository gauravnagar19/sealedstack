# Known limitations and caveats

sealedstack is **v0.5 (pre-release)** and has not yet been applied against live
accounts or a real server. This page is the candid list of things that will bite
you: what does not work, what only works with caveats, and the environmental
requirements behind both. For the capability matrix see status (private)
and the verification log (private); for the design rationale behind a
few of these, see [decisions.md](maintainers/decisions.md).

- **Secret sources are read-only (D-038).** The engine only reads secrets; it never writes them. Tunnel tokens and other generated values live only in encrypted state. If you are upgrading from a build that wrote them to the source, `tofu state rm` the old `module.secrets_*` resources before the first apply (the release notes list the addresses).

- **R2 access uses the `aws` CLI.** Database backups/restores and the state backend read/write R2 through the bundled `aws` CLI (the engine signs S3 via it); unlike `gh`/`jq`/`yq`/`curl`, `aws` stays in the toolbox image.

## Threat model: what an app repo's writers can and cannot do (D-063)

Anyone who can run a workflow in an app repo can run its release job, and the job
holds a Dokploy API key. What that key allows is the boundary.

**Since D-063 the key is not the admin key.** `apply` creates one Dokploy *member*
per app (role `member`, e-mail `deploy-<app>@dokploy.invalid`, no mail is ever sent)
and publishes **that member's** key as the repository secret
`SEALEDSTACK_DOKPLOY_API_KEY`. The member is limited, in Dokploy's own permission
model (pinned to v0.30.8), to that app's projects, environments and compose
services. `apply` proves it every time: the key must read the app's own composes and
be refused on every other app's, and a key that is not scoped is never published.

| | A writer of one app repo |
|---|---|
| Read or change the env (secrets, database URLs) of **another app** | No |
| Deploy, stop or delete **another app**, or touch the panel's settings, users, servers, registries | No |
| Read the admin key, the other apps' keys, or the Tailscale / Cloudflare / R2 credentials | No (none is in an app repo) |
| Reach the servers over SSH | No (`tag:release` may use TCP 3000 only, D-057) |
| Read or change **its own** app's env and compose, and deploy it | Yes, by design |
| Make **its own** compose run with host access (privileged, `docker.sock`, host paths, host namespaces) | **Yes, and Dokploy cannot prevent it** (below; planned fix: D-065) |

**What remains open.** Dokploy has no permission for "deploy this compose but do not
change what it contains": a member who may deploy a compose may also change its
`composeFile`, its `command` and its source (`compose.update` accepts all of them,
and needs the `canCreateServices` flag on the member). A compose file or command
with a privileged container or the Docker socket is root on the host, and the lint
below cannot stop someone who calls Dokploy's API directly with the key instead of
using the release job. So **a writer of an app repo can still, in effect, take over
the server that runs that app.** What D-063 removes is the *other* apps, the panel
and the infrastructure credentials, and the lint adds two checks:

- the **release job** lints the rendered deploy compose before anything reaches
  Dokploy (`app.yml`; refuses `privileged`, `docker.sock` and other runtime sockets,
  host paths other than `/etc/localtime`, `network_mode`/`pid`/`ipc`/`uts`/`userns_mode: host`,
  `cap_add` beyond a short list, `devices`, and `security_opt` that turns off confinement);
- the **infra repo** applies the same rules to every app's compose in `check`,
  `plan` and `apply` (an app that fails is refused, and nothing is deployed for it).

Both only see the compose file. They are a guard rail against mistakes and
against the release job being misused, not a boundary against someone who holds the key.

**Closing it fully** needs app repos to hold *no* Dokploy credentials: the release
job would record a GitHub Deployment and the infra repo's CI (`tag:ci`) would perform
the deploy, reading the compose from the app repo at the tagged commit through the
lint. That needs a trigger path (the infra CI watching deployments, or an approved
`/sealedstack release <app> <tag>` command) and is a follow-up, not part of this
release. **Planned: D-065**, to be built after the first live run and before anyone
else gets write access to an app repo. Until then: treat every writer of an app repo as an administrator of the
server that runs that app, and keep apps with different trust levels on different servers.

Two further details. An app that shares its repository with another app receives the
key of the last one published. And the member's password and key live only in the
encrypted state (never in the secret source or in git), like the Dokploy admin password.

## Who may release an app (DR-11)

By default an app's release is not gated: D-025 gives app CI the release, so anyone
who can run the app repo's release workflow (a writer) can deploy to production.
For one owner that is what you want. To narrow it, list the people under
`release.approvers` in the app's `sealedstack.yaml`:

```yaml
release:
  approvers: [you, co-owner]
```

The release job then refuses a run started by anyone else (`github.actor`), before
it touches Dokploy. Know what this is and is not:

- The list is read from the **default branch**, never from the branch the run was
  started on, so choosing a branch cannot add yourself to it.
- A writer who can push to the default branch can still edit the list (and the
  workflow). Branch protection, which private repositories on GitHub Free do not
  have, is what would stop that. So the list limits who may *start* a release; it
  is not a boundary against a writer who is determined to bypass it.
- It does not change what a releaser can do once running (see the threat model above).

## The state lock (D-062)

CI write commands (`apply`, `up`, `restore`, `add-server`, `server sync`) and
`--break-glass` runs take **one lock**, the object `sealedstack/<infra name>/state.lock`
in your R2 state bucket, with a conditional write, so they exclude each other.
It records the holder, the CI run URL and an expiry (2 hours). Reads (`check`,
`plan`, `status`, `drift`) never lock.

- A run that finds the lock held stops and prints the holder, how long it has
  been held and `sealedstack --force-unlock <id>`.
- A lock past its expiry (a runner killed mid-apply never releases it) is taken
  over by the next write run, with a warning naming the previous holder. The
  hand-over is read-then-delete-then-create, not one atomic step, so two runs
  that start in the same second on the same expired lock could still both
  proceed; the previous holder is dead by then and the window is a few requests.
- The Status issue gets a problem comment when a lock has been held for more than an hour.
- OpenTofu's own state lock still protects the tofu phases on top of this.

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
| `doppler` | Doppler's REST API with a read-only service token ([D-066](maintainers/decisions.md)); no CLI needed |
| `infisical` | Infisical's REST API with a read-only machine identity ([D-067](maintainers/decisions.md)); no CLI needed |
| `onepassword`, `pass` | the provider's own CLI |

**`gcp` and `azure_kv` are write-only.** They have no read path in the toolbox
image, so `secrets_get` fails with a clear message (no CLI in the image; use the
provider console or export the value) instead of silently returning empty. If
you select one of them you must export those values as environment variables for
anything that reads secrets back.

The CLI adapters (`onepassword`, `pass`) require **their
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
- no successful `production` deployment is recorded in the app repo and no Dokploy
  lookup is available to identify the currently-deployed version (the retired
  `SEALEDSTACK_RELEASED_VERSION` variable is only a deprecated fallback).

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

## The engine image owner is your repository owner

`sealedstack setup` and the interactive wizard (`debug init`) name the engine image
`ghcr.io/<repository owner>/sealedstack:<version>`; there is no placeholder owner.
Pass `--owner` to use another namespace. The tool `Dockerfile` has no default
base image either: pass `--build-arg TOOLBOX_IMAGE=...` (the release workflow and
`just tool` do).

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

## Netcup reinstall has not run live (D-070)

`reinstall` was built from Netcup's published API description and has only been exercised against a
test double. Unverified until the first live run: that the `scp` device-flow client accepts a
refresh-token exchange from CI, that the install task finishes in the stated states, and that
Netcup runs the first-boot script as root before SSH is first used (the script sets the root
password and allows password login for the bootstrap only). The root password travels once, in the
install request, to Netcup over TLS; the bootstrap turns root and password logins off afterwards
(D-061). The refresh token lapses after about 30 days unused, and sealedstack cannot rotate it
for you because the secret source is read-only ([renewing it](netcup-reinstall.md#renewing-the-token)).

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
