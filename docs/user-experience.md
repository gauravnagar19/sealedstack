# sealedstack: User experience (final)

| | |
|---|---|
| Status | **Final (v1 UX): signed off by the owner on 2026-10-11.** This document is authoritative for everything a user sees and does. [spec.md](spec.md) §6 defers to it. Changes need a new decision in [decisions.md](decisions.md). |
| Date | 2026-10-11 |
| Decisions | D-034 to D-047 ([decisions.md](decisions.md)). Amended on 2026-10-12 by D-056: the caller workflow in §4 step 2 also grants `packages: read`. Amended on 2026-10-13 by D-057: app release jobs join the tailnet as `tag:release` (Dokploy API only), not `tag:ci`; by D-058: the bot identity for now is a fine-grained personal access token (§4 step 4 checklist); by D-059: `drift` is also a manual command, and scheduled drift (the hourly run after `status` and the `drift.yml` workflow) is off unless the repository variable `SEALEDSTACK_DRIFT_ENABLED` is `true` (§8 table, §13); by D-060: the app release caller written by the onboarding PR (§5, §7) also grants `deployments: write`, and each release is recorded as a GitHub Deployment of `production` in the app repo; and the Setup checklist (§4 step 4) has a row for the `tag:release` OIDC trust credential, which `check` also verifies for the infra repo and each app repo. |

Mockups use GitHub wording; GitLab equivalents are in §11. `<v>` means the pinned sealedstack version (e.g. `0.6.0`), and `<owner>` the sealedstack publisher.

---

## 1. Principles

1. **Zero installs.** A browser and a git hosting account. Everything runs in CI.
2. **The pull request is the interface.** You change things by merging a reviewed PR. sealedstack proposes PRs; it never pushes to `main`.
3. **Ask once, check everything, explain every failure.** Prerequisites are checked before anything changes. Every problem shows what happened, why, and the exact fix.
4. **One vocabulary.** The same command names in CI jobs, PR comments and the optional local terminal.
5. **Safe by default.** Destroys, restores and laptop-side changes require explicit confirmation. Data is never overwritten silently.
6. **One place to look.** A pinned **Status** issue in your infra repo shows health, versions, backups and drift, and notifies you of problems.

## 2. Who uses it

| Persona | Wants | Touches |
|---|---|---|
| **Owner/operator** (you) | Servers and apps running, secure, cheap, with low effort | The infra repo (PRs, Setup/Up/Add jobs, approvals, Status issue) |
| **App developer** | Ship code; release and roll back quickly | Their app repo (merge, then click *Release*) |
| **Contributor** | Improve sealedstack | The sealedstack repo (devcontainer, `just` aliases) |
| **Evaluator** | Know quickly whether this fits | README, the 2-minute overview, a demo |

## 3. Time budgets (targets the implementation must meet)

| Journey | Your hands-on time | Wall time |
|---|---|---|
| First setup (§4), including creating accounts and tokens | **≤ 45 min** | ≤ 60 min |
| Up: empty server → hello app live | 2 clicks | **≤ 15 min** |
| Add app (§6) | ≤ 5 min plus reviewing 2 PRs | ≤ 10 min |
| Day-2 config change (§8) | Edit + merge + approve | ≤ 5 min |
| Release / rollback (§7) | 1 click each | **≤ 2 min / ≤ 1 min** |
| Upgrade sealedstack (§9) | Review + merge one PR | ≤ 10 min |

---

## 4. Journey A: first setup (from nothing)

### Step 1: create the infra repo

Create an **empty private repo** with any name (yours: `my-infra`). In repo settings, enable **"Allow GitHub Actions to create and approve pull requests"** (needed once, so the first Setup PR can be opened before the bot exists).

### Step 2: add two files and commit to `main`

`sealedstack.version`
```
0.6.0
```

`.github/workflows/sealedstack.yml` (copied from the docs)
```yaml
name: sealedstack
on:
  workflow_dispatch:
    inputs:
      command:
        type: choice
        options: [setup, check, plan, apply, up, add-server, add-app, status, restore, drill, drift]
      args:
        description: "e.g. --name server-1 (IP and root password come from the secret source)"
        required: false
  pull_request:
  push:
    branches: [main]
  issue_comment:
    types: [created]
  schedule:
    - cron: "17 * * * *"          # hourly status (+ drift if SEALEDSTACK_DRIFT_ENABLED=true)
jobs:
  sealedstack:
    uses: <owner>/sealedstack/.github/workflows/infra.yml@v0.6.0
    with:
      command: ${{ inputs.command }}
      args: ${{ inputs.args }}
    secrets: inherit
    permissions: { contents: write, pull-requests: write, issues: write, id-token: write, packages: read }
```

The version appears twice: `sealedstack.version` (used by Codespaces, the launcher and GitLab) and the workflow's `@v0.6.0`, because GitHub requires a fixed ref there. **`check` fails if they differ**, and the upgrade bot always bumps both in one PR (§9).

*Shortcuts that produce the same two files:* "Use this template" on GitHub, the project template on GitLab, or `sealedstack init` in a Codespace.

### Step 3: run Setup

**Actions → sealedstack → Run workflow → command: `setup`**, with `args`:

```
--domain example.com --email you@example.com --admin admin
--server server-1 --provider netcup --secrets bitwarden
```

Setup asks for nothing else. All other config has safe defaults you can edit in the PR.

### Step 3b: the secret names are written into your config (D-050)

Setup writes the **exact key name of every secret** into `config/`, so you can always see which Bitwarden entry feeds which setting:

```yaml
# config/stack.yaml
secrets:
  source: bitwarden
  project: infra                          # Bitwarden Secrets Manager project
  keys:                                   # logical name → key in the secret source
    cloudflare_api_token:          CLOUDFLARE_API_TOKEN
    tailscale_oauth_client_id:     TAILSCALE_OAUTH_CLIENT_ID
    tailscale_oauth_client_secret: TAILSCALE_OAUTH_CLIENT_SECRET
    state_passphrase:              STATE_PASSPHRASE
    bot_private_key:               SEALEDSTACK_BOT_PRIVATE_KEY
```

```yaml
# config/servers.yaml
servers:
  server-1:
    provider: netcup
    role: control
    bootstrap:
      method: password                    # password | key
      ip_secret: SERVER_IP_SERVER_1       # key in the secret source, never the IP itself
      password_secret: ROOT_PASSWORD_SERVER_1
```

You can rename any key (e.g. to match an existing Bitwarden entry). The checklist and every error message show both names: *"`cloudflare_api_token` (Bitwarden key `CLOUDFLARE_API_TOKEN`) not found"*.

### Step 4: the Setup PR and its live checklist

Setup opens **PR #1 "sealedstack setup"** with `config/stack.yaml`, `servers.yaml`, `apps.yaml` (containing the `hello` app), `.devcontainer/devcontainer.json` and a README. It then posts this comment, which updates itself:

```markdown
## sealedstack setup checklist                                   (updated 14:02 UTC)

Progress: 3 of 9 ready. Fix the ❌ items below, then comment `/sealedstack check`.

| | Prerequisite | Status |
|---|---|---|
| ✅ | Config files | valid |
| ✅ | Version pins match (sealedstack.version = workflow ref = 0.6.0) | |
| ✅ | Domain `example.com` is on Cloudflare | |
| ❌ | **Secret source: Bitwarden** | `BWS_ACCESS_TOKEN` is not set in this repo's `production` environment. → [How to create it (2 min)](…) |
| ❌ | **CLOUDFLARE_API_TOKEN** (in Bitwarden, project `infra`) | not found. → [Create token with exactly these permissions](…): Account · Cloudflare Tunnel: Edit · Access: Apps and Policies: Edit · Workers R2 Storage: Edit · Zone · DNS: Edit · Zone WAF: Edit · Bot Management: Edit · Single Redirect: Edit |
| ❌ | **Tailscale OAuth client** (`TAILSCALE_OAUTH_CLIENT_ID` / `_SECRET`) | not found. → [Create client: scopes `policy_file`, `auth_keys`, `devices:core`](…) |
| ⏳ | Tailscale policy imported into `config/tailscale-policy.hujson` | waits for the OAuth client |
| — | Tailscale OIDC trust credential for app releases (`tag:release`) | not needed until an app has its own repository. → manual step in the Tailscale console, Settings → Trust credentials → Credential → OpenID Connect: Issuer GitHub; Subject `repo:you/<app>:environment:production`; Scope `auth_keys` write; Tag `tag:release`. Then put its Client ID and Audience in `config/stack.yaml` (D-057 amendment) |
| ❌ | **STATE_PASSPHRASE** (≥ 16 chars) | not found. → generate one in Bitwarden |
| ❌ | **Server server-1**: `ip_secret` → `SERVER_IP_SERVER_1`, `password_secret` → `ROOT_PASSWORD_SERVER_1` (from `servers.yaml`) + port 22 open | not found in Bitwarden. → add both keys; [Netcup: open TCP 22 temporarily](…) |
| ❌ | **Bot identity** (fine-grained token `GH_TOKEN`) | not set. → **[Create the token (pre-filled)](…)**: only this repo and your app repos; Contents, Pull requests, Issues, Variables, Secrets, Workflows (read and write), Deployments, Metadata (read). Add it as the repository secret `GH_TOKEN`. *(D-058 amendment: the GitHub App, "Create sealedstack-bot (1 click)", comes later.)* |
| — | Optional: Clerk keys | not needed yet (no app uses Clerk) |

Merge this PR when everything above is ✅. Nothing has been changed outside this repo yet.
```

### Step 5: create the prerequisites

Follow the links. Each one opens the exact page, with the permission list. Put the values in Bitwarden (the default secret source); only `BWS_ACCESS_TOKEN` goes into GitHub.

Comment **`/sealedstack check`** on the PR whenever you like. The checklist updates in place. When the Tailscale OAuth client exists, `check` **imports your current tailnet policy** into `config/tailscale-policy.hujson` as a new commit on the Setup branch, so you can review it.

If your policy still has Tailscale's default allow-all rule, the checklist shows:

```markdown
| ❌ | **Tailscale policy is allow-all** | Your rule `* → *` lets every device reach every server port, so servers would not be sealed. → Set `mesh.narrow_allow_all: true` in `config/stack.yaml` (this PR) to rewrite it to `autogroup:member → autogroup:member:*`. Your devices keep reaching each other; servers are reachable only through sealedstack's rules. [Preview of the change](…) |
```

### Step 6: merge PR #1

When all items are ✅, merge. This is your approval of the configuration. Nothing outside the repo has changed yet.

### Step 7: run Up

**Run workflow → command: `up`.** The job waits for approval in the `production` environment (you click **Approve**), then posts its progress to the job summary and the Status issue:

```
sealedstack up · my-infra · v0.6.0
 [1/8] check ........................................ ✅ 9/9 ready         12s
 [2/8] state bucket (R2, encrypted) ................. ✅ created            8s
 [3/8] edge: tunnel, DNS, Access, WAF, policy ....... ✅ 14 created        41s
 [4/8] server server-1: bootstrap over its public IP ✅ admin user, ssh hardened, joined tailnet   1m52s
 [5/8] server server-1: baseline ................... ✅ firewall, docker, dokploy v0.30.8          6m10s
 [6/8] platform: hello ............................. ✅ deployed           58s
 [7/8] verify ...................................... ✅ ports closed · hello → 302 Access · /.env → 403   20s
 [8/8] status issue ................................ ✅ #2 pinned
Done in 10m31s.  Next: close TCP 22 on Netcup (now unused) → https://…/netcup-firewall
          Open https://hello.example.com (log in with you@example.com)
```

If any step fails, the summary names the step, the cause and the fix. **Re-running `up` continues** from where it stopped.

---

## 5. Journey B: add another server

- **Providers with an API** (e.g. Hetzner): add the server to `config/servers.yaml` in a PR, merge, approve. `apply` creates it and bootstraps it. No IP or password is needed.
- **Existing machines** (Netcup, any-ssh): add the server to `servers.yaml` with its `bootstrap.ip_secret` / `password_secret` key names (`add-server` proposes `SERVER_IP_<NAME>` / `ROOT_PASSWORD_<NAME>`, with `<NAME>` upper-cased and `-` → `_`), put those two keys in the secret source, open TCP 22, then **Run workflow → `add-server`** with `--name server-2`. It opens a PR adding the server to `servers.yaml` (name, provider and role only; never the IP), with the bootstrap result as a comment. Merge the PR to record it. Afterwards you can delete both secrets: the server is reached only by its tailnet name.

---

## 6. Journey C: add an app

**Run workflow → `add-app`** with `--name example-app --repo you/example-app --host test.example.com --access owner-only`. Two PRs appear:

**In `my-infra`, PR "Add app example-app":**
```markdown
Adds example-app on server-1 at https://test.example.com (login: owner-only).

Plan: 9 to create · 0 to change · 0 to destroy
  + tunnel ss-example-app · DNS test.example.com · Access app (owner-only)
  + Dokploy project, compose, domain, connector · Postgres 16 (example) · backups every 6h → R2
Waiting on: you/example-app#14 (onboarding) to be merged first.
```

**In `example-app`, PR "Onboard to sealedstack":**
```markdown
This PR prepares example-app for sealedstack. Review each change:
1. sealedstack.yaml (new): web = ui:80, health /api/health; needs Postgres 16 with 6-hourly backups
2. docker-compose.yml:
   - removed `ports:` from ui and api (traffic arrives through the tunnel; ports would be exposed)
   - removed the postgres service (sealedstack runs a managed, backed-up database)
   - added image: ghcr.io/you/example-app-{ui,api}:${APP_VERSION} (build: kept for local dev)
3. .github/workflows/release.yml (new): build on merge; manual Release; replaces deploy.yml
Local development is unchanged: `docker compose up --build` still works.
```

**Merge the app PR first.** That's the order the infra PR states, and the infra plan re-checks automatically. Then merge the infra PR and approve. The app now exists, waiting for its first release. The Status issue shows `example-app · not released yet → run Release`.

---

## 7. Journey D: release and roll back (app developer)

- **Every merge to `main`** builds images and creates `v0.0.N` (about 2–3 min). Nothing deploys.
- **To release:** in the app repo, **Actions → release → Run workflow → tag `v0.0.N`**. It deploys in about 1 minute and reports the result to the job summary and the Status issue.
- **To roll back:** run Release again with the previous tag (about 1 minute; no rebuild).
- **Each release is recorded** as a deployment of the `production` environment in the app repo (Code page → Deployments), with the version and the servers. If a server is ever rebuilt, `up` redeploys the version of the latest successful deployment (D-060). A failed release is recorded as failed and never used for that.
- **A failed release** fails the job and leaves the previous version running. The summary shows the deploy log tail and the likely cause.

---

## 8. Journey E: day-2 changes

**Example: make `hello` public.** Edit `config/apps.yaml` in a branch (the web editor is fine) and open a PR. The plan comment appears:

```markdown
## sealedstack plan · PR #7
1 to create · 1 to change · 1 to destroy
  ~ Access: hello.example.com  owner-only → public (bypass)
  + Access bypass app for hello.example.com
  - Access app "sealedstack: hello.example.com"
⚠ This makes hello.example.com reachable by anyone on the internet (still behind WAF and bot rules).
<details>Full OpenTofu plan</details>
```

Merge it, then approve in `production`. Apply runs and comments the result on the PR.

**Destroying anything stateful** (a database, a backup bucket) is **blocked**. The plan shows it in red, with instructions to add an explicit `allow_destroy:` entry in the same PR if you really mean it.

---

## 9. Journey F: upgrade sealedstack

A bot (Renovate/Dependabot, configured by Setup) opens **"Update sealedstack 0.6.0 → 0.6.1"**, which bumps `sealedstack.version` **and** the workflow ref together. The PR contains:
- the release notes, with **security fixes highlighted**,
- the plan under the new version (normally "0 changes"),
- any manual steps needed, as a checklist.

Merge it when ready. Nothing upgrades on its own. The Status issue warns if you're on a version affected by an advisory or more than 6 months old.

---

## 10. Journey G: when something goes wrong

### The Status issue (always pinned)

```markdown
# 🟢 my-infra status                                   updated 15:17 UTC · sealedstack 0.6.0 (latest)

| Server | Mesh | Dokploy | Updates |
|---|---|---|---|
| server-1 | 🟢 online | 🟢 v0.30.8 | 🟢 none pending |

| App | URL | Version | Last release | Health |
|---|---|---|---|---|
| hello | hello.example.com | image | — | 🟢 |
| example-app | test.example.com | v0.0.14 | 2h ago ✅ | 🟢 /api/health 200 |

| Backups | Newest | Last restore drill |
|---|---|---|
| example-app/example | 🟢 3h ago | 🟢 Sun 03:00 OK |

Drift: 🟢 none · Advisories: 🟢 none
```

**Problems are posted as comments,** so you get an email or a phone notification. Each comment states the cause and the fix:

| Event | Comment says | You do |
|---|---|---|
| **Drift** (someone changed something by hand) | What differs; "run `apply` to restore, or adopt the change in a PR" | Merge a PR, or run Up |
| **Backup older than 2 intervals** | Which DB; the last error | Follow the fix; `drill` to verify |
| **Server offline** | Last seen; whether the apps are still serving | Provider console; `up` after recovery |
| **Health check failing** | App, status code, last release | Roll back (§7) |
| **Restore drill failed** | Error | Investigate before you need the backup |

### Restore a database

**Run workflow → `restore`** with `--app example-app [--backup <key>]`. Approve in `production`, then **type the app name to confirm**. It refuses a non-empty database unless you add `--force`. The app is stopped during the restore and restarted after.

### Break-glass (GitHub or CI is down)

Open the infra repo in a **Codespace**, or use the launcher with a container runtime:

```
sealedstack status                     # read-only, always allowed
sealedstack plan                       # read-only
sealedstack apply --break-glass        # only if your checkout equals origin/main;
                                       # takes the state lock, asks you to type "my-infra",
                                       # posts an audit comment on the Status issue
```

When even that isn't possible, use the Dokploy panel (over Tailscale) and the provider console.

---

## 11. Journey H: moving to GitLab

1. Push the infra repo to GitLab.
2. Replace the workflow file with `.gitlab-ci.yml`:
   ```yaml
   include:
     - project: <owner>/sealedstack
       ref: v0.6.0
       file: /ci/gitlab/infra.yml
   ```
3. In `config/stack.yaml`, set `forge: gitlab` and `ci: gitlab-ci` (in an MR).
4. Add `BWS_ACCESS_TOKEN` as a masked, protected CI/CD variable, and create the project access token for the bot.

Config, state, servers, Cloudflare and Bitwarden are unchanged. The same journeys apply, with these mappings: PR → **merge request**; Actions "Run workflow" → **Pipelines "Run pipeline"** with a `SEALEDSTACK_COMMAND` variable; environment approval → **protected environment** (or a manual job for maintainers); comment commands work the same on MRs; the Status issue becomes a **GitLab issue**.

---

## 12. Reference

### 12.1 Commands (CI input, PR comment and local, all the same)

| Command | Where | Approval | Writes? |
|---|---|---|---|
| `setup` | Run workflow | — | Opens the Setup PR only |
| `check` | `/sealedstack check`, auto on PRs | — | No (may commit the imported policy to the Setup branch) |
| `plan` | Auto on PRs | — | No |
| `apply` | Auto after merge to `main` | `production` | Yes |
| `up` | Run workflow / `/sealedstack up` on a merged PR | `production` | Yes |
| `add-server` | Run workflow | `production` | Yes + PR |
| `add-app` | Run workflow | — | Opens PRs only |
| `status` | Hourly + after every apply | — | Updates the Status issue |
| `drift` | Run workflow / `/sealedstack drift` any time; hourly right after `status` only when the repository variable `SEALEDSTACK_DRIFT_ENABLED` is `true` (D-059) | — | No: a problem becomes a Status-issue comment |
| `restore` | Run workflow | `production` + typed name | Yes |
| `drill` | Weekly + Run workflow | — | Throwaway DB only |

**Comment commands** run only for users with **write** access to the repo; others get a polite refusal.

**Approval (D-052).** Write commands (`apply`, `up`, `restore`, `add-server`, `server sync`) run only when the commit is merged through a PR (a direct push to `main` is refused) **and** an approver has agreed — either the run was dispatched by a user in `stack.yaml: approvers`, or an approver commented **`/sealedstack approve`** on the PR after the latest plan comment. This is enforced by the engine, so it works on private GitHub Free repos where the `production` environment has no required reviewers; the environment is still used as a second gate where your plan supports it.

### 12.2 What lives where (user view)

| You edit | You never edit |
|---|---|
| `config/*.yaml`, `config/tailscale-policy.hujson` (via PRs) | State (encrypted in R2) |
| The secret source (Bitwarden) | Generated tokens and passwords (in state) |
| `sealedstack.version` + workflow ref (via the bot PR) | App repo CI variables (published by apply) |

### 12.3 Message format (every error, everywhere)

```
✖ Cloudflare token is missing a permission
  What:  Creating the Access app for hello.example.com was refused (403).
  Why:   CLOUDFLARE_API_TOKEN lacks "Access: Apps and Policies: Edit".
  Fix:   Edit the token at https://dash.cloudflare.com/profile/api-tokens and add it,
         then comment /sealedstack check.
  Docs:  https://…/prerequisites#cloudflare
```

### 12.4 What a user never does

- Install OpenTofu, Ansible, Python, Docker or any other tool, or know which versions are used.
- Push to `main`, or run `apply` from a laptop (except a guarded break-glass).
- Copy tunnel tokens, R2 keys, database passwords or Dokploy keys anywhere.
- SSH to a server on a public IP after setup.
- Edit DNS, Access apps or Tailscale rules by hand (drift is reported).

---

## 13. Defaults finalized by this document

| Choice | Final default |
|---|---|
| Infra CI file name | `.github/workflows/sealedstack.yml` (GitHub), `.gitlab-ci.yml` include (GitLab) |
| Approval environment name | `production` |
| Status schedule | Hourly (minute 17); restore drill weekly, Sunday 03:00 UTC |
| Scheduled drift | Off. The hourly run does `status` only; set the repository variable `SEALEDSTACK_DRIFT_ENABLED=true` to add `drift` right after it (D-059). Manual `drift` is always available |
| Backup alert | Newest backup older than 2× its schedule |
| Version-age warning | > 6 months, or any applicable advisory |
| Comment command prefix | `/sealedstack` |
| Bot name | `sealedstack-bot` (a fine-grained personal access token for now, D-058; a user-owned GitHub App later, D-039) |
| Bootstrap secrets per existing server | Key names declared in `servers.yaml` (`bootstrap.ip_secret` / `password_secret`, default `SERVER_IP_<NAME>` / `ROOT_PASSWORD_<NAME>`); values only in the secret source (D-049, D-050) |
| Secret key names | Declared in `stack.yaml: secrets.keys` (logical → source key); defaults written explicitly by Setup (D-050) |
| First app | `hello` (`traefik/whoami`, owner-only) |
