# Spec: Infra repos, build modes and releases

| | |
|---|---|
| Status | **Revision 2: ready to build.** Revision 1 (M0–M2) is code complete with hardening open. Revision 2 changes **how users run sealedstack**: CI-first, platform-neutral, no local installs (§3, §6, §7, §8 and D-034 to D-044). |
| Date | 2026-10-11 |
| Decisions | D-022 to D-044 in [decisions.md](decisions.md) (§16 lists the new ones) |
| Prerequisite reading | [spec.md](spec.md) (base design, in the private repo) |
| Reference setup | Infra repo `my-infra`; reference app `example-app`, then a public app `shop` |

sealedstack is a **versioned engine** (a container image) that turns a few credentials and a small config into a sealed server running your apps. Users **install nothing**: the engine runs in their CI, driven by their **infra repo**. Every change is a reviewed pull or merge request. Where this spec and `spec.md` disagree, this spec wins. §15 lists the `spec.md` sections to update.

---

## 1. Goals

1. **Zero installs.** A user needs a browser, a git hosting account and the prerequisite credentials. No Docker, OpenTofu, Ansible, `just` or Python on their machine. An optional local launcher exists for power users and emergencies (§6.6).
2. **One setup, then one action.** Once the prerequisites exist, **Up** takes an empty server to the hello app live, and **Add app** onboards an app end to end.
3. **Prerequisites are few, checked up front, and explained.** A preflight checks everything before anything changes, and every problem comes with the exact fix (§7).
4. **Platform-neutral.** The engine knows nothing about GitHub or GitLab. Thin adapters handle CI and repo operations (§8). GitHub ships first; GitLab plugs in without engine changes.
5. **Every change is reviewed and traceable.** Changes go through PRs/MRs; nothing writes to `main`; apply runs only after merge and approval (§9).
6. **One clear owner per setting:** infra decides *where and how an app is exposed*; the app declares *what it needs* and *owns its releases* (§4, §5).
7. **Fast, cheap, reproducible releases:** build once in CI, deploy the same image everywhere, roll back in seconds (§4).
8. **Safe by default:** nothing destroys or overwrites production unless asked explicitly (§13).

## 2. Non-goals (this track)

- A hosted control plane or web UI for sealedstack itself.
- A native (non-container) toolchain on user machines.
- Building on a separate build server (Dokploy's build-server feature doesn't support Docker Compose; roadmap).
- Database HA and multi-server database apps (D-029).
- New providers beyond the golden path (D-027). The GitLab adapter is **designed** here but built after GitHub (D-012).

---

## 3. How it fits together

```
 ┌──────────────── sealedstack (the product) ─────────────────┐
 │ engine image  ghcr.io/<owner>/sealedstack:<version>         │  OpenTofu, Ansible, bws, sops… pinned inside
 │ CI adapters   github-actions (reusable workflows) · gitlab-ci (include templates)
 │ forge adapters github · gitlab   (PR/MR, CI variables, read app manifests)
 └──────────────▲───────────────────────────────▲─────────────┘
                │ pins version + CI snippet      │ release workflow / template
 ┌──────────────┴───────────────┐       ┌───────┴──────────────────────────────┐
 │ <name>-infra (user)          │       │ app repos (example-app, shop)           │
 │ config/ · sealedstack.version│──────►│ code · compose · sealedstack.yaml    │
 │ CI file · tailscale policy   │ PRs + │ release CI                           │
 └──────────────────────────────┘ vars  └──────────────────────────────────────┘
            │ engine runs in CI (Docker on the runner, never on the user's machine)
            ▼
   Cloudflare · Tailscale · secret source · R2 state · servers (Dokploy)
```

| Repo | Owns | Never contains |
|---|---|---|
| **sealedstack** | Engine code (Ansible, OpenTofu, scripts, schemas, registry), the engine image, CI and forge adapters, docs | Any user's domain, servers, apps, emails or IPs |
| **Infra repo** (`<name>-infra`, name chosen by the user) | `config/`, `sealedstack.version`, the CI file, the Tailscale policy | Secrets, engine code, state |
| **App repo** | Code, compose, `sealedstack.yaml`, the release CI file | Server names, hostnames, access policies, infra repo references |

**Links point one way (D-023):** the infra repo names app repos; app repos never name an infra repo. What app CI needs is **published** into the app repo's CI variables by the infra repo (§9.4).

---

## 4. Building and releasing apps (unchanged from revision 1)

### 4.1 Options compared

| | **A. Build in CI on merge, promote on release** (default, D-030) | B. Build on release tag | C. Build on the server |
|---|---|---|---|
| Release time | **About 30–60 s** (pull and restart) | 2–5 min | 3–10 min |
| CI minutes | About 1–3 min per merge with layer cache | About 1–3 min per release | 0 |
| Production load | Pull only | Pull only | CPU/RAM spike on the box serving users |
| Server needs repo access | No | No | Yes |
| Rollback | Seconds | Seconds | Rebuild the old tag |
| Reproducible | ✅ | ✅ | ❌ |

`build.mode: server` (C) stays available as an opt-in.

### 4.2 `build` block in the app manifest

```yaml
build:
  mode: ci                 # ci (default) | server
  platforms: [linux/amd64] # default: derived from the infra's servers
```

| Mode | Compose rule | What Dokploy gets | Release does |
|---|---|---|---|
| `ci` | Every service with `build:` also has `image: <registry>/<owner>/<repo>-<service>:${APP_VERSION}` | A rendered deploy compose (raw source) with `build:` removed | Update the deploy compose (from the tag) and `APP_VERSION`, then deploy |
| `server` | `build:` allowed | Git source at the tag; Dokploy builds | Set branch to the tag, then deploy |

### 4.3 Release pipeline (shipped per CI adapter)

**On push to `main`:**
1. Mint `v0.0.X+1`.
2. Build each `build:` service in parallel with layer cache.
3. Push `:<version>` and `:sha-<short>`.
4. Prune old images.
5. Target ≤3 min with a warm cache.
6. **Serialized per repo** (concurrency group) so two merges never mint the same version.

**On manual release (tag input):**
1. Refuse if the images are missing.
2. Join Tailscale as `tag:ci`.
3. Render the deploy compose at the tag.
4. Update only release-owned fields (§5).
5. Deploy, then wait for a **new** deployment ID to reach `done`. Anything else fails the job.
6. Target ≤2 min. Rollback is releasing the previous tag.

The adapters provide the same behaviour: on GitHub a reusable workflow (`uses: <owner>/sealedstack/.github/workflows/app.yml@vX`), on GitLab an `include:` template (§8).

### 4.4 `registry` slot (D-033)

`ghcr` is the golden path; `gitlab` (the GitLab Container Registry) arrives with the GitLab adapter; `dockerhub`, `quay` and generic `oci` are planned.

---

## 5. Ownership contract inside Dokploy (D-032, unchanged)

| Field | Owner |
|---|---|
| Project, compose, server, domains, connector, database, Redis, backups, registry, infra env keys | **infra** (apply) |
| `APP_VERSION`; compose file (`ci`) / Git branch (`server`) | **release** (release pipeline; the initial value is set by apply) |

**Rules:**
- Env is merged, never overwritten.
- Release-owned fields are ignored after creation.
- Unknown keys are kept and warned about.
- Apply never deploys never-released `ci` apps.
- **After a server rebuild,** apply redeploys each `ci` app at its **last released version**, recorded by the release pipeline as a forge variable `SEALEDSTACK_RELEASED_VERSION`. Rebuilt servers come back without a manual re-release (review F7).

---

## 6. The user experience

> **The authoritative, final UX is [user-experience.md](user-experience.md)** (journeys, mockups, command reference, defaults). This section keeps the engineering requirements behind it; where they differ, user-experience.md wins.

### 6.1 Prerequisites: few, and checked

The design minimises what the user creates by hand. Anything that can be **derived or generated** is, and generated values live only in the encrypted state (D-038).

| # | Prerequisite | Kind | Notes |
|---|---|---|---|
| 1 | **Server** `SERVER_IP_<NAME>` plus `ROOT_PASSWORD_<NAME>` (or an SSH key installed at order time), per existing server (D-049) | secret | Used once; password login is disabled afterwards (D-040) |
| 2 | **`CLOUDFLARE_API_TOKEN`**: one token, plus the account ID (not secret) | secret | Docs give the exact permission list, plus a pre-filled token-creation link where Cloudflare supports one. **The R2 S3 keys are derived from this token** (key ID = token ID; secret = SHA-256 of the token value), so there are no separate R2 keys to create. |
| 3 | **Tailscale OAuth client** (`TAILSCALE_OAUTH_CLIENT_ID`/`_SECRET`, scopes listed), plus the CI login trust (OIDC) | secret + guided step | The OAuth client mints server auth keys and manages the policy; no 90-day API keys |
| 4 | **`STATE_PASSPHRASE`** (≥16 characters) | secret | Docs give a one-line generator; store it in the secret source |
| 5 | **Your SSH public key** | **config** (not secret) | `config/stack.yaml: admin.ssh_keys: [...]` |
| 6 | **Bot identity**: a user-owned GitHub App (one-click manifest flow; GitLab: a project or group access token) | secret | §9.3 |
| 7 | **Secret source access**: `BWS_ACCESS_TOKEN` (Bitwarden, default) | the **only** CI secret | Everything else is read from the source |
| 8 | Optional: Clerk keys, per app | secret | Only for `user_auth: clerk` apps |

### 6.2 Journey: from nothing to running (browser only)

1. **Create an empty repo** (any name, e.g. `my-infra`) on GitHub or GitLab.
2. **Add two files:** `sealedstack.version` (e.g. `0.6.0`) and the CI snippet for your platform, copied from the docs (about 10 lines; §8). Commit to `main`.
   - *Shortcuts:* the GitHub "Use this template" button, the GitLab project template, or `docker run … sealedstack init` locally (§6.6). All produce the same two files.
3. **Run the *Setup* job** and fill in its inputs (domain, owner email, admin user, first server name and provider, secret source). Setup:
   - Writes `config/` on a branch and opens a **Setup PR/MR**.
   - Posts a **living checklist** comment: every prerequisite from §6.1 with ✅/❌, the exact fix and a link for each ❌, and the permission list for each token.
4. **Create the missing prerequisites** following the checklist. Comment `/sealedstack check` on the PR/MR (or push any change) to re-run preflight. The checklist updates in place until everything is ✅.
5. **Merge the Setup PR/MR.** This is the approval of the configuration.
6. **Run *Up*** (or comment `/sealedstack up` on the merged PR, if enabled). It waits for approval in the protected `production` environment, then runs:
   ```
   preflight → state → edge (tunnel/DNS/Access/WAF/policy) → server bootstrap → host baseline
            → platform (Dokploy, hello app) → verify (acceptance checks) → status
   ```
   Every step is safe to re-run: a failed run is fixed and simply run again.
7. **The *Status* issue** is created and pinned in the infra repo (§6.4).

### 6.3 Journey: adding an app

1. **Run *Add app*** with the repo, hostname and access. sealedstack opens two PRs/MRs:
   - **Infra repo:** the app entry in `apps.yaml`, with the plan as a comment.
   - **App repo:** `sealedstack.yaml`, the release CI file and compose fixes (remove `ports:`, add `image:`), each explained in the description.
2. **Review and merge both.** The infra apply runs after approval and publishes the app's CI variables.
3. **Release from the app repo** (manual tag release, as today).

### 6.4 Visibility: the Status issue

A scheduled job (hourly by default) and every apply update a **pinned issue** in the infra repo. Updating an issue isn't a commit, and issues work the same on GitHub and GitLab. It shows:
- **Servers:** online over the mesh, Dokploy healthy, OS security updates pending.
- **Apps:** URL, released version, last deploy result, health check.
- **Backups:** age of the newest successful backup per database, and the last restore drill.
- **Drift:** whether `plan` shows changes nobody made through a PR.
- **Versions:** the pinned sealedstack version, the newest available, and security advisories.

Problems are posted as **comments** on that issue, so the owner gets normal email or mobile notifications for free.

### 6.5 Commands (one vocabulary everywhere)

The engine CLI is `sealedstack <noun> <verb>`. CI jobs, `/sealedstack` comment commands and the optional local launcher all use it:

| Command | CI job / comment | Does |
|---|---|---|
| `setup` | *Setup* | Writes `config/` and opens the Setup PR/MR with the checklist |
| `check` | `/sealedstack check` | Preflight: every prerequisite, with fixes (also runs first in every other command) |
| `up` | *Up* / `/sealedstack up` | Converges everything (§6.2 step 6) |
| `plan` | automatic on PRs/MRs | Human summary (creates, changes, **destroys in red**), then the full plan |
| `apply` | automatic after merge, with approval | Applies the merged config |
| `server add <name>` / `server sync` | *Add server* / scheduled | Bootstrap a new server / re-assert the host baseline |
| `app add` | *Add app* | §6.3 |
| `db restore <app>` / `db drill <app>` | *Restore* / scheduled | Restore requires typing the app name to confirm, and refuses a non-empty DB unless forced |
| `status` | *Status* (scheduled) | Updates the Status issue (§6.4) |
| `debug …` | (none) | Internals (`render`, `publish`, `providers`) for troubleshooting |

**Rules:**
- **Every failure says what happened, the likely cause, the fix and a docs link.**
- **Destroys need explicit confirmation:** a typed confirmation, or `--allow-destroy=<address>`.
- **`--json` output** is available for `check`, `plan` and `status`.
- **`just`** exists only in the sealedstack repo, as contributor aliases with **no logic**: each recipe calls the CLI.

### 6.6 Local use (optional: reading, debugging, emergencies) (D-045, D-046)

**Users never need a local setup.** CI does all configuration. Local use exists for three cases only: debugging (`plan`, `check`), visibility (`status`), and emergencies when the forge or CI is down.

**What a local run may do (D-045):**

| Command | Locally |
|---|---|
| `check`, `plan`, `status`, `db drill` | ✅ always (read-only) |
| `apply`, `up`, `server add/sync` | ❌ refused by default: changes come from merged `main` via CI |
| The same with `--break-glass` | ✅ guarded (see the conditions below) |
| `db restore` | Guarded exactly like break-glass |

A `--break-glass` run proceeds only when **all** of these hold:
1. The local checkout equals `origin/main` exactly (clean tree, same commit).
2. It takes the state lock, so CI can't run at the same time.
3. The operator types the infra name to confirm.
4. It posts an audit comment on the Status issue: who, when, commit, and a summary of changes.

The engine detects a local run by the absence of the CI adapter's environment.

**How to run locally (D-046), in order of preference:**

| Option | User installs | Notes |
|---|---|---|
| **Codespaces / devcontainer** (recommended) | **Nothing** (browser) | The infra repo ships `.devcontainer/devcontainer.json` pointing at the pinned engine image, generated by Setup. Open the repo in a Codespace and get a terminal with `sealedstack` ready. VS Code can use the same file locally if a container runtime is present. |
| **Launcher** | The `sealedstack` launcher (`brew install <owner>/tap/sealedstack` or an install script) plus any Docker-compatible runtime (Docker Desktop, OrbStack, Colima, Podman) | The launcher reads `sealedstack.version` and runs that engine image. It installs no other tools. |
| ~~Native (no container)~~ | — | **Not planned.** It would need sealedstack to manage OpenTofu, Ansible and Python on each OS; unnecessary now that local use is optional and read-mostly. |

**Locally, the engine writes files only; it never commits or pushes** (D-043). Credentials come from the user's environment or secret source.

### 6.6a The toolchain lives inside the engine, never on the user's machine

The user installs **none** of these. They exist only inside the engine image, which runs on CI runners, in a Codespace, or under the launcher:

| Inside the engine | Why it's needed | After the Python migration (D-047) |
|---|---|---|
| OpenTofu (+ about 4 providers for the default setup) | Cloudflare, Tailscale, Dokploy, R2 | Stays (core) |
| Ansible (+ Python, `ansible.posix`) | Server configuration | Stays (core) |
| OpenSSH client | Reaching servers | Stays |
| `bws` | Bitwarden secrets | Replaced by the Bitwarden SDK |
| `sops` + `age` | Only with `secrets.source: sops` | Stays (optional) |
| `gh`, `jq`, `yq`, `curl`, `aws`, `postgresql-client` | Glue in the bash scripts | **Removed:** direct API calls and Python libraries; DB restores run in a container on the server |

**Why not let Homebrew install these:** Homebrew installs the latest version of each dependency, shared machine-wide, and `brew upgrade` changes it silently. Infrastructure tools must be pinned per sealedstack version and identical in CI and locally, which is why the engine ships them in its image.

### 6.7 Versions of the tools inside the engine

- **Every tool, OpenTofu provider and server component is pinned per sealedstack release.** Renovate opens bump PRs in the sealedstack repo; CI and e2e must pass; then a release ships.
- **Users upgrade by merging a bot PR** in their infra repo that bumps `sealedstack.version`. The PR includes the release notes and a plan preview. Nothing updates on its own.
- **Security fixes ship as patch releases.** `check` and the Status issue warn when the pinned version is affected or very old. cloudflared is bumped at least quarterly (Cloudflare supports only the last 12 months of releases).
- **The engine image is signed** (cosign keyless) with an SBOM, multi-arch. CI adapters verify the signature before running.

---

## 7. Preflight (`check`)

Runs first in every command, and standalone. It verifies, **without changing anything**:

| Area | Checks |
|---|---|
| Config | Schemas, cross-file rules, compose rules per build mode, **all of `validate.py`** (so plan/apply can't skip guards) |
| Secrets | Every name the selected providers need exists in the source; values pass basic checks (passphrase length) |
| Cloudflare | The token is valid and **has each required permission** (each permission is checked by a harmless API call); the zone exists; Zero Trust is enabled; R2 is reachable with the derived keys |
| Tailscale | OAuth works; the policy file is present (or `mesh.policy: unmanaged`); the merged policy passes its own tests (including the allow-all detection, §10); `mesh.tailnet` is set |
| Forge | The bot identity can open PRs/MRs and set variables on every listed app repo |
| Servers | New servers: TCP 22 is reachable from the runner and root login works. Existing servers: reachable over the tailnet as `tag:ci` |
| Versions | The engine version isn't deprecated or affected by an advisory |

Output is the checklist in §6.2 (markdown for PR/MR comments, plus `--json`).

---

## 8. Platform neutrality: `ci` and `forge` adapters (D-035)

```yaml
# config/stack.yaml
forge: github          # github | gitlab
ci: github-actions     # github-actions | gitlab-ci
```

**The engine contract:** the engine only reads config, environment and secrets, and writes reports (markdown and JSON) to files. Everything platform-specific goes through two adapter interfaces:

| Interface | Operations |
|---|---|
| **ci** | Run an engine command in a job; protected-environment approval; schedule; comment commands (`/sealedstack …`); log masking; release pipeline (§4.3); OIDC login to Tailscale where supported |
| **forge** | Read a file from an app repo at a ref; open a PR/MR with a branch and files; comment on or update a PR/MR or issue; set and read CI variables and secrets on an app repo; create or update the pinned Status issue |

| Concept | GitHub (build now) | GitLab (designed, build after) |
|---|---|---|
| Engine job | Reusable workflow running the engine image | `include:` a template; jobs run **in** the engine image |
| Proposals | Pull requests | Merge requests |
| Approval before apply | Environment with required reviewers | Protected environment with deployment approval (paid tiers), or a manual job limited to maintainers (free) |
| CI secrets | Environment secrets | Masked, protected CI/CD variables |
| Bot identity | User-owned GitHub App | Project or group access token |
| Tailscale login from CI | OIDC workload identity | GitLab ID tokens, **if** Tailscale's OIDC trust accepts the GitLab issuer (verify); otherwise an OAuth client secret |
| Registry | GHCR | GitLab Container Registry |

**Moving platforms:** push the infra repo to the new forge, swap the CI snippet, change `forge`/`ci`, and recreate the one CI secret (`BWS_ACCESS_TOKEN`). Config, state, servers, Cloudflare and Bitwarden are unaffected.

---

## 9. Change flow and access

### 9.1 Rules (D-037)

- **Nothing writes directly to `main`.** Setup, Add app and version bumps all open PRs/MRs. Users enable branch protection: a PR/MR is required, and the plan check must pass.
- **Apply runs only from `main`, after merge,** gated by the protected `production` environment.
- **Apply never commits.** Machine-produced data goes to encrypted state (R2), forge CI variables, or the Status issue.
- **CI is the only normal writer (D-045).** Local runs are read-only, plus the guarded `--break-glass` mode (§6.6). All writes, from CI or break-glass, take the state lock.

### 9.2 What lives where

| Data | Where | Written by |
|---|---|---|
| Intent (domain, servers, apps, access, Tailscale policy, admin SSH keys) | Infra repo, git | The user, or a merged sealedstack PR/MR |
| App needs and release CI | App repo, git | The user, or a merged onboarding PR/MR |
| User-provided credentials | The secret source | The user |
| Generated values (tunnel tokens, DB passwords, Dokploy key, derived R2 keys) | Encrypted state | apply |
| What app CI needs | App repo CI variables and secrets | apply (forge adapter) |
| Health and drift | The Status issue | `status` |

### 9.3 Bot identity (D-039)

- **GitHub (default):** a user-owned GitHub App, created by Setup's one-click manifest flow and installed only on the infra repo and the chosen app repos.
  - **Permissions:** contents (write, for PR branches), pull requests, issues, variables, secrets, metadata.
  - **Tokens are short-lived,** minted per job. PRs opened by the App **do** trigger plan workflows, which the built-in `GITHUB_TOKEN` can't do.
  - **Alternative:** a fine-grained personal token limited to the same repos and permissions.
- **GitLab:** a project or group access token with `api` scope, or a bot user.
- **The credentials (App private key, token)** live in the secret source like everything else.

### 9.4 What apply publishes to each app repo

`SEALEDSTACK_COMPOSE_IDS`, `SEALEDSTACK_DOKPLOY_URL`, `SEALEDSTACK_TS_*` (OIDC identifiers), `SEALEDSTACK_REGISTRY` and `SEALEDSTACK_RELEASED_VERSION` (written by release, §5) as variables. `SEALEDSTACK_DOKPLOY_API_KEY` goes in as a secret, in a protected environment.

---

## 10. Tailscale policy: "your file, our block" (D-026, D-041)

1. **Setup imports the current policy** verbatim into `config/tailscale-policy.hujson` (in the Setup PR/MR, so you review it). With `mesh.policy: unmanaged`, sealedstack prints its block for manual pasting instead.
2. **At plan time, sealedstack merges its block:**
   - **`tagOwners`:** adds `tag:server` and `tag:ci`.
   - **`grants`/`acls`:** appends these rules:
     - admin → `tag:server` (all ports)
     - **`tag:ci` → `tag:server` on TCP 22 and 3000** (CI bootstraps and syncs servers over the tailnet, and calls the Dokploy API)
     - DB rules later (D-029)
   - **Tests:** appended:
     - admin reaches `tag:server:22`
     - `tag:ci` reaches `tag:server:22` and `:3000`
     - **`tag:ci` can't reach `tag:server:5432`**
     - **an untagged member device can't reach `tag:server:3000`**
   - **Every other section** passes through untouched.
3. **Allow-all detection:** a rule that accepts to `*` or `autogroup:*` (Tailscale's default `* → *`) means the servers aren't sealed. Preflight **fails** with instructions. With `mesh.narrow_allow_all: true`, the exact `* → *` is rewritten to `autogroup:member → autogroup:member:*`, and the diff is shown in the plan.
4. **`mesh.tailnet` is required;** all addresses are fully qualified.

---

## 11. Secrets (D-038)

```yaml
# config/stack.yaml
secrets:
  source: bitwarden     # bitwarden (default) | ci (GitHub/GitLab CI secrets) | sops | env
```

- **One fixed list of secret names** per selected provider; `check` prints exactly what's needed.
- **Key names are declared in config (D-050):** `stack.yaml: secrets.keys` maps each logical secret (e.g. `cloudflare_api_token`) to its key in the source, and each existing server's `servers.yaml: bootstrap.ip_secret` / `password_secret` name its bootstrap keys. Setup writes the defaults explicitly; users may rename keys; messages show both the logical and the source name. App secrets default to `<app>.<NAME>` and may be remapped per app in `apps.yaml: secrets:`.
- **sealedstack only reads secrets; it never writes to the source.** Everything it generates lives in the encrypted state. This makes every source equally easy to support and keeps sealedstack out of your password manager's write path.
  - **Migration from revision 1:** remove the tunnel-token write-back into Bitwarden (`secrets_bitwarden` writes in the edge stack) and keep those tokens in state only.
- **Bitwarden (default)** moves with you across forges. The CI platform holds only `BWS_ACCESS_TOKEN`.
- **`ci`** reads the CI platform's secrets, passed to the engine as environment variables. It's the simplest option, but it's tied to the forge.
- **`sops`** reads an encrypted file in the infra repo, with the key held in the CI secret. **`env`** reads environment variables; it's meant for local and test runs.

---

## 12. Servers: first login (D-040)

- **A new server** is bootstrapped from CI over its **public IP on TCP 22**, using `ROOT_PASSWORD_<NAME>` (default) and `SERVER_IP_<NAME>`, both from the secret source (D-049; never git, job inputs or logs) or an SSH key installed at order time (`server.bootstrap: password | key`).
- **The provider firewall** must allow TCP 22 during bootstrap. `check` verifies reachability and prints the provider's rule table. Afterwards, `server add` tells the user (or, for providers with an API, does it itself) to close it.
- **Bootstrap:** creates the admin user with the SSH keys from config; disables password and root login (verified with `sshd -T`); joins Tailscale with a single-use `tag:server` key minted through the OAuth client and passed via `--auth-key=file:`; locks the firewall. From then on, all access is over the tailnet.
- **The root password is useless after bootstrap.** Docs suggest deleting it from the secret source.

---

## 13. Safety requirements (carried from reviews; each needs a test)

| ID | Requirement |
|---|---|
| N3 | Required secrets abort on missing values; one fetch per source per run; source errors are surfaced |
| N4 / §10 | Policy guard and allow-all detection |
| N5 / N6 | Deploys wait for a new deployment ID; rebuilds redeploy (§5) |
| N8 | e2e isolated: its own config dir and state prefix; reverse teardown; hard failures |
| N9 | Env merge; release fields never overwritten |
| C2 | Strict render; `prevent_destroy` on stateful resources; destroys need explicit confirmation |
| H10 | No secret in any process argv (DB password via a 0600 env file; Tailscale key via `file:`) |
| §7 | **plan/apply/up always run the full preflight** (including `validate.py`) |
| F5 | Release pipeline serialized per repo |

The private verification log records only CI-produced or recorded live results.

---

## 14. How to build it, and the milestones in order

Build **top to bottom**; do not start a milestone before the previous one **passes**. Each milestone has a binary pass/fail. Live results are recorded in verification log (private); a milestone is only done when its pass criteria are demonstrated (CI run or a recorded live run), not when the code merely exists.

### 14.1 Builder prerequisites

| Need | Why |
|---|---|
| Docker + buildx locally | build/test the engine image |
| A **scratch** GitHub org (and, for M9, a scratch GitLab group) | run Setup/Up/Add app without touching real repos |
| A **dedicated** Cloudflare zone and Tailscale tailnet (not your personal one, D-026) | M4 and e2e |
| One throwaway server (e.g. Hetzner CX22, ~€0.01/h) | e2e + M4; never the real Netcup box until M4 |
| A Bitwarden org + one machine token | the secret source |
| A Cloudflare account with a payment method | Zero Trust + R2 on free plans |

### 14.2 Build order and dependencies

`M0 → M6 → M7 → M8 → M3 → M4 → M5`, and `M9` after `M7`. M0–M2 (revision 1) are code complete; M3+ use the revision-2 (CI-first) flow, so M6/M7 must land first.

### 14.3 Per-milestone pass/fail

#### M0 — Green and safe (revision 1 hardening)
- **Build:** the §13 safety requirements that are code-only, with tests.
- **Pass:** CI is green including `bats tests/`; the §13 items have tests that fail before the fix and pass after; the private verification log records only CI-produced results.
- **Fail:** any CI job red; any §13 item without a test; a verification entry not backed by a run.
- **Verify:** the CI `ci.yml` run is green on a PR.

#### M6 — Engine CLI and preflight
- **Build:** the `sealedstack <noun> <verb>` CLI (§6.5); `check` (§7) with markdown + `--json`; `up`; R2 keys derived from the Cloudflare token; Tailscale OAuth client (no 90-day keys); SSH keys in config; read-only secret sources (§11) incl. the state-only migration; `just` becomes logic-free aliases; **move orchestration from bash to Python (D-047)** in order `check` → `plan`/`apply`/`up` → `server` → `db` → `status`, dropping `gh`/`jq`/`yq`/`curl`/`aws`/`postgresql-client` from the image as each lands; local-run rules + `--break-glass` (§6.6); `.devcontainer/` generated by Setup.
- **Pass:** every §7 check has a unit test with mocked APIs; `up` re-run after a mid-way failure finishes cleanly; local `apply` without `--break-glass` is refused; break-glass refuses a dirty or out-of-date checkout and posts the audit comment (mock forge); the migrated commands have pytest coverage and **no bash orchestration remains** for them.
- **Fail:** a §7 check without a test; `up` not idempotent; local `apply` succeeds without break-glass; any migrated command still shells out to a bash script.
- **Verify:** `pytest -q` on the CLI package; a scripted `up` twice; the local-refusal cases.

#### M7 — GitHub CI and forge adapters
- **Build:** the GitHub `ci` + `forge` adapters and reusable workflows: *Setup, Check, Plan, Apply, Up, Add server, Add app, Status, Drift*; the GitHub App manifest flow; the PR checklist comment; release-pipeline concurrency.
- **Pass (in the scratch org):** empty repo + the two files → the *Setup* job opens a Setup PR with a checklist → all ✅ → merge → *Up* reaches the `server add` step against a stubbed server → the *Status* issue appears and is pinned.
- **Fail:** Setup needs a local install; the checklist doesn't gate merge; *Up* isn't re-runnable; no Status issue.
- **Verify:** a recorded run in the scratch org, linked from `verification.md`.

#### M8 — Engine image release
- **Build:** the signed, multi-arch engine image on `v*` tags with an SBOM; Renovate config; the version-bump PR template for infra repos.
- **Pass:** a tag produces a **verifiable signed** image (cosign) with a readable SBOM; the adapters refuse to run an unsigned image.
- **Fail:** image unsigned; no SBOM; adapters run regardless.
- **Verify:** `cosign verify` on the published tag; a deliberate unsigned pull is rejected.

#### M3 — Reference app (the public template)
- **Build:** prepare `examples/app-template` (remove `ports:`, add `image:`, add `sealedstack.yaml` + release CI), then onboard it end to end with *Add app* in the CI/e2e environment (a mocked or scratch forge, not a real app repo).
- **Pass:** *Add app* opens both PRs; they merge cleanly; a merge to `main` builds and pushes images; `validate` passes for the template app.
- **Fail:** *Add app* fails to open either PR; a manual compose fix is needed.
- **Verify:** the two merged PRs and a successful image build, recorded in the private verification log.

> A real app repo migration is a private, owner-run task and is not part of the public repo or this milestone's acceptance.

#### M4 — Live v0.1 (owner-run)
- **Build:** nothing new; run the §6.2 journey for real.
- **Pass:** `spec.md` §15 v0.1 criteria 1–7, **plus**: zero local installs used; release ≤2 min; rollback ≤1 min; `apply` after a release changes nothing; a rebuilt server restores the database and redeploys the last released version; a fresh-server rebuild works from git + secret source + R2 only.
- **Fail:** any criterion; any local install required; a rebuild that needs a manual re-release; `apply` that touches `APP_VERSION`.
- **Verify:** record the acceptance run in `verification.md`; flip golden-path providers to `supported` (`registry.py release-status`).

#### M5 — shop
- **Pass:** shop live at the apex + `www` with Clerk, public access, redirect working; `spec.md` §15 v0.2 criteria.
- **Fail:** any of the above.

#### M9 — GitLab adapter
- **Build:** the GitLab `ci` + `forge` adapters (§8 GitLab column).
- **Pass:** the M7 acceptance, on GitLab.com.
- **Fail:** any M7 criterion.

### 14.4 Definition of done (every milestone)
1. Code merged; 2. the pass criteria demonstrated in CI or a recorded live run; 3. the private verification log updated with the run; 4. affected providers' `status` bumped only after M4 (golden path → `supported`).

Golden-path providers become `supported` only after M4.

---

## 15. Changes to make in `spec.md`

| spec.md section | Change |
|---|---|
| §3 Repositories | Replace with §3 here |
| §4 Slots | Add `registry`, `forge` and `ci`; maturity labels |
| §5 Config | `secrets.source`, `forge`, `ci`, `admin.ssh_keys`, `server.bootstrap`, `mesh.narrow_allow_all` |
| §6 Secrets | Replace with §11 here |
| §7 Host | First login per §12 |
| §8 Edge | Policy merge per §10; R2 keys derived |
| §9 Platform | §5 here, including rebuild redeploy |
| §10 Commands | §6.5 here |
| §13 Security | CI credentials model (§9.3, D-041) |
| §14 Prerequisites | §6.1 here |
| §15 Delivery | §14 here |
| §16 Risks | R8 GHCR/Actions pricing; R9 Dokploy key scoping; R10 HuJSON fidelity; **R11 CI holds deploy power (mitigated by environments, short-lived tokens, OIDC)**; **R12 GitLab approval rules may need a paid tier** |

---

## 16. Decisions added by revision 2

**D-034** (CI-first engine), **D-035** (`ci`/`forge` adapters), **D-036** (universal infra-repo bootstrap), **D-037** (PR/MR-only changes, approval-gated apply), **D-038** (read-only secret sources, Bitwarden default), **D-039** (bot identity), **D-040** (first login), **D-041** (CI SSH over Tailscale), **D-042** (one CLI vocabulary; `just` contributor-only), **D-043** (local launcher writes files only), **D-044** (minimal prerequisites: derive or generate everything else), **D-045** (CI is the only normal writer; local is read-only plus guarded break-glass), **D-046** (local delivery: Codespaces/devcontainer and the launcher; no native mode), **D-047** (the engine's orchestration moves from bash to Python). Full text in [decisions.md](decisions.md).

## 17. Future direction (not in this track)

**A pull-based agent.** An agent on the control server watches the infra repo and reconciles on change, like Flux or Argo CD. CI would then hold **no** infrastructure credentials, and it would need only read access to git. It's more secure and fully forge-neutral, but it can't fix the server it runs on, so bootstrap and disaster recovery still need the push path. Revisit after M4, once the push model has been proven live.

## 18. Open questions

1. **Q-A:** Does Dokploy support project-scoped API keys? (If not, app CI holds the shared key; mitigated by a protected environment.)
2. **Q-B:** Can GHCR pulls use a fine-grained token?
3. **Q-C:** Does the Dokploy provider support raw compose and registries, or is the API needed?
4. **Q-D:** Does Tailscale's OIDC trust accept GitLab CI ID tokens?
5. **Q-E:** Does Cloudflare support a pre-filled token-creation link with permission groups? If not, docs show the list.
6. **Q-F:** Can the Setup job import the Tailscale policy before the OAuth client exists? If not, the import happens on the first `/sealedstack check` once the client is present.
