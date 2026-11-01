# Decision Log

> **Maintainers.** This page explains how sealedstack is built and why. It is **not** instructions for
> setting up or running a sealedstack infrastructure, and parts of it describe designs that were later
> changed (the decision log says which). To use sealedstack, start at [docs/](../README.md).


Each entry records what was decided, why, and what was rejected. Superseded entries stay in the log and are marked as such.
Format: **D-NNN — title** · date · status.

---

### D-001 — Zero inbound ports ("sealed origin") · 2026-10-11 · accepted
**Decision:** No public inbound port is open on any server, including 22, 80 and 443. App traffic arrives through an outbound tunnel; admin traffic arrives over a mesh VPN.
**Why:** Bots were reaching the origin directly by IP. A port that isn't open cannot be attacked.
**Rejected:** Allowlisting Cloudflare IP ranges on 80/443. The origin would still be visible to scanners, and the ranges change.

### D-002 — Cloudflare Tunnel + Cloudflare Access at the edge · 2026-10-11 · accepted
**Decision:** Cloudflare Tunnel handles ingress, and Cloudflare Access gates internal hostnames.
**Why:** Free (Access is free up to 50 users), needs no app code, drops unauthenticated traffic before it reaches the server, and is reusable for every app.
**Rejected:** Auth only at the app level. Every bot request would still cost server resources, and each app would need to integrate it separately.

### D-003 — Clerk for end-user auth (public apps) · 2026-10-11 · accepted
**Decision:** Apps with public sign-up, waitlist and user management (shop) use Clerk. Cloudflare Access keeps guarding internal surfaces only (admin, staging, Dokploy).
**Why:** The operator is not an auth specialist. A managed provider ships security fixes without any action from us (Better Auth had several High-severity CVEs in 2026). Clerk is SOC 2 Type 2 and free up to 50k monthly retained users.
**Accepted trade-offs:** Data is hosted in the US only (EU transfers go under the Data Privacy Framework), and there is vendor lock-in. Lock-in is limited by verifying Clerk tokens in a single middleware and mirroring users into the app's own DB via webhooks.
**Rejected:** Better Auth (we would own patching), self-hosted Zitadel, Logto or Authentik (operational burden), and building our own.

### D-004 — A public app may be served at the apex · 2026-10-11 · accepted
**Decision:** A public app may use the apex domain, with `www` redirected to it with a 301. It is `access: public`, protected by in-app auth plus WAF and bot rules (a public app is only reachable if it doesn't require gated access).

### D-005 — Close Docker's UFW bypass with `DOCKER-USER` · 2026-10-11 · accepted
**Decision:** UFW `after.rules` and `after6.rules` drop new connections that arrive on the WAN interface in the `DOCKER-USER` chain, and allow `tailscale0`.
**Why:** Docker-published ports bypass UFW's INPUT rules, so on some hosts the provider firewall would otherwise be the only thing protecting published container ports.

### D-006 — Bitwarden for secrets · 2026-10-11 · accepted
**Decision:** Bitwarden Password Manager holds human break-glass credentials, Bitwarden Secrets Manager holds machine secrets, and the Bitwarden SSH agent holds the operator's SSH key.
**Why:** It's free, gives one tool for humans and machines, has an official GitHub Action and an Ansible lookup, and isn't a self-hosted dependency that would have to exist before the servers it configures.
**Rejected:** HashiCorp Vault (heavy to operate; the hosted HCP Vault Secrets reached end of life in July 2026), AWS Secrets Manager (about $0.40 per secret per month), GCP Secret Manager and Azure Key Vault (cheap, but each adds a cloud account with billing and permissions to secure), Infisical (fewer free-plan identities), and SOPS (planned as the second implementation, not the first).

### D-007 — OpenTofu for APIs, Ansible for hosts, `just` as the entrypoint · 2026-10-11 · accepted
**Decision:** OpenTofu manages the Cloudflare, Tailscale, R2 and Dokploy APIs. Ansible configures hosts. Neither does the other's job.
**Why:** No single tool does both well. OpenTofu is open source (MPL licence, where Terraform uses BSL) and encrypts state natively, which matters because the state holds tunnel tokens.
**Rejected:** Terraform (licence; no native state encryption), Pulumi (adds an SDK and a state service, and still needs Ansible), Ansible-only (weak at tracking and detecting drift in API resources).

### D-008 — No IP addresses or secrets in config · 2026-10-11 · accepted
**Decision:** A server is identified by its name, which is also its Tailscale MagicDNS hostname. The public IP is a one-time argument to `server-up` and is never stored.
**Why:** Config stays portable, nothing breaks when an IP changes, and nothing in the repo is sensitive.

### D-009 — Keep Dokploy as the platform · 2026-10-11 · accepted
**Decision:** Dokploy provides compose deploys, Traefik, managed databases, S3 backups, monitoring, notifications and multi-server deploys.
**Why:** Rebuilding these features ourselves isn't worth it. Its main weakness (config stored in its own database) is addressed by managing it with OpenTofu.
**Rejected:** Plain compose plus Traefik via CI (loses the database, backup and monitoring features), Coolify (equivalent; no reason to switch), Kamal (no database or backup management).

### D-010 — One Dokploy panel, other servers as remote workers · 2026-10-11 · accepted
**Decision:** Exactly one `control` server runs the panel. Other servers are Dokploy remote servers reached over Tailscale.
**Why:** One place to look; workers stay lean (the panel needs about 2 GB of RAM). If the panel is down, deploys stop but running apps keep serving, and the panel can be rebuilt from code.
**Rejected:** One panel per server (RAM overhead on every box; several dashboards to check).

### D-011 — Community Dokploy provider, with a REST fallback · 2026-10-11 · accepted
**Decision:** Use `vanillauys/dokploy`, pinned exactly. If it fails the v0.1 gate, `platform_dokploy` calls the Dokploy REST API instead, keeping the same module contract.
**Why:** The provider has a single maintainer, so the risk is contained behind the module boundary.

### D-012 — Slots: generic contracts, one implementation each · 2026-10-11 · accepted
**Decision:** Every external dependency (compute, mesh, ingress, gate, secrets, backup, platform, user_auth) is a slot with a written contract. The MVP ships one implementation per slot. A contract is generalised only when a second implementation is added.
**Why:** The project can be open-sourced and made pluggable later without over-engineering it now.

### D-013 — One Cloudflare tunnel per app · 2026-10-11 · accepted (supersedes "one tunnel per server" from the initial Netcup guide)
**Decision:** Each app gets its own tunnel. Every server listed in the app's `servers:` runs one connector for it.
**Why:** Adding a server to an app's list gives it a replica with free failover. A per-server tunnel would route a hostname to a server that might not run that app.
**Cost:** One small cloudflared container per app per server (about 20 MB).

### D-014 — OpenTofu owns the entire Tailscale ACL · 2026-10-11 · accepted
**Decision:** The `tailscale_acl` resource manages the whole policy file. Manual edits in the Tailscale console will be overwritten.
**Why:** A partially managed ACL drifts, and tags and grants are security-critical.

### D-015 — App CI owns releases; sealedstack owns placement · 2026-10-11 · accepted
**Decision:** sealedstack creates services with auto-deploy off. Each app repo's CI (GitHub Actions → Tailscale `tag:ci` → Dokploy API) deploys versions.
**Why:** This keeps the existing, tested manual-tag pipeline, and keeps infra changes separate from release changes.

### D-016 — OpenSSH over Tailscale, not Tailscale SSH · 2026-10-11 · accepted
**Decision:** Admins use OpenSSH with a key from the Bitwarden agent, reachable only on `tailscale0`.
**Why:** If Tailscale's control plane or its SSH feature is unavailable, the provider's console plus OpenSSH remains a known recovery path.
**Revisit:** Tailscale SSH (no keys at all) is a reasonable future option.

### D-017 — App manifest in the app repo, placement in the config repo · 2026-10-11 · accepted
**Decision:** `sealedstack.yaml` in each app repo declares its needs (web service, databases, backups, secret names). `config/apps.yaml` declares servers, hostnames and access.
**Why:** Apps become portable: anyone can deploy them on their own sealedstack without editing the app repo.

### D-018 — Dokploy-managed databases with backups to R2 · 2026-10-11 · accepted
**Decision:** Databases declared under `needs:` are created as Dokploy-managed databases, not inside the app's compose file, and are backed up on a schedule to an S3-compatible destination (R2 for the MVP).
**Why:** This gives Dokploy's built-in scheduled backups and restore, and R2 is free up to 10 GB.

### D-019 — The project is named `sealedstack` · 2026-10-11 · accepted
**Why:** It says the security promise (no open ports) in the name, and no GitHub repo used it as of 2026-10-11.
**Rejected:** infraploy, hushport, tunnelstack, boxstrap, darkport, noport.

### D-020 — Single repo for now; split `config/` at v1.0 · 2026-10-11 · superseded by D-022
**Decision:** Generic code and the user's `config/` share one private repo until the open-source release. At release, `config/` moves to a private `sealedstack-config` repo.
**Why:** The fastest path to a working MVP, with clean boundaries already in place for the split.

### D-021 — v0.1 acceptance runs on a freshly installed server · 2026-10-11 · accepted
**Decision:** v0.1 acceptance runs against a freshly installed server (any provider), not an already-configured one. Short downtime during that window is acceptable.

### D-022 — Three repo types; the infra repo is split out now · 2026-10-11 · accepted (supersedes D-020)
**Decision:** `sealedstack` is the tool: public, versioned, shipped as a container image, never forked. Each user has their **own infra repo** that holds only config and pins a sealedstack version. App repos hold code, compose, `sealedstack.yaml` and release CI.
**Why:** Users can upgrade the tool without merging forks, a user's config never leaks into the open-source repo, and the same tool serves many infra repos.

### D-023 — The infra repo owns placement and access; app repos own needs · 2026-10-11 · accepted
**Decision:** Links point one way: the infra repo's `apps.yaml` lists the apps, and app repos never reference an infra repo. The infra repo publishes what app CI needs (compose ID, CI identity settings) to the app repo as GitHub variables and secrets.
**Why:** Security decisions (what's public, who can log in) stay in one reviewed place, and app repos stay portable.

### D-024 — Apps deploy as prebuilt images from a registry · 2026-10-11 · accepted
**Decision:** App CI builds images on release and pushes them to GHCR, tagged with the release version. Dokploy only pulls `image:${APP_VERSION}`; it never clones source or builds on the server.
**Why:** It removes the need for repo access on the server (private repos, review H7). Releases are immutable, rollback is "deploy the previous tag", the small VPS spends no CPU on builds, and no source code sits on the server.
**Note:** example-app currently builds on the server (`build: ./ui`), so it needs migrating.

### D-025 — Releases stay with app CI; sealedstack never touches release fields · 2026-10-11 · accepted
**Decision:** Release fields (`APP_VERSION` and the deployed tag) belong to the app's CI. sealedstack manages only the env keys it owns (DB URLs, injected secrets) and ignores drift on release fields.
**Why:** It fixes review N9, where an `apply` run reset the tag to `main`, and it keeps D-015.

### D-026 — Tailscale: one tailnet, "your file, our block" · 2026-10-11 · accepted (refines D-014)
**Decision:** On first run, the user's existing Tailscale policy is imported verbatim into the infra repo (`config/tailscale-policy.hujson`). sealedstack adds and tests only its own tags and rules. Applying refuses to run until that file exists.
**Why:** sealedstack can lock servers down without breaking the user's other devices, and every policy change is reviewable in git.
**Rejected:** a separate tailnet for servers (the admin laptop would have to switch tailnets); printing rules for manual pasting (drifts out of date, nothing checks it).

### D-027 — Provider maturity labels; unimplemented providers move to `contrib/` · 2026-10-11 · accepted (refines D-012)
**Decision:** Registry status is one of `supported` (tested live), `experimental` (code exists, never run) or `planned`. Providers with no implementation move to `contrib/`. Only the golden path is `supported`.

### D-028 — The infra repo name is the user's choice · 2026-10-11 · accepted
**Decision:** `sealedstack init` asks for the infra repo name (e.g. `my-infra`). The reference app is validated on it first, then further apps are onboarded.

### D-029 — Multi-server DB apps come after v0.2 · 2026-10-11 · accepted
**Decision:** shop replicas sharing one Postgres (Tailscale-reachable with TLS, plus ACL rules) are wanted, but they're scheduled after v0.1/v0.2 and don't block launch. Until then, validation rejects multi-server apps with a database.

### D-030 — Default build mode is "CI on merge, promote on release" · 2026-10-11 · accepted
**Decision:** Apps build images once per version on every push to `main` and push them to a registry; a release deploys an existing image with no rebuild. `build.mode: server` (build on the Dokploy host) stays as an opt-in.
**Why:** fastest releases (pull and restart), the same image from test to production, no build load on the production VPS, no repo access needed on the server, and seconds-long rollback.
**Rejected:** build-on-release (slower, rebuilds), build-on-server as the default (CPU/RAM spikes, rebuild per deploy, source on the box).

### D-031 — sealedstack ships as a versioned, signed tool image with a CLI · 2026-10-11 · accepted
**Decision:** sealedstack is published as a container image (`ghcr.io/<owner>/sealedstack:<version>`) containing the toolchain and the code, with a `sealedstack <command>` CLI entrypoint. Each user runs it from their own infra repo, which pins the version in `sealedstack.version` and calls it via a generated `bin/sealedstack`. Only Docker is needed on the host.
**Why:** users never fork or clone sealedstack; upgrades are a one-line version bump; the tool is reproducible and signed.
**Superseded by this:** the "clone and fill in" model and `just` as the user entrypoint (`just` remains a contributor convenience).

### D-032 — One writer per Dokploy field; env is merged, never overwritten · 2026-10-11 · accepted
**Decision:** Each Dokploy compose field has exactly one writer: infra (project, domains, connector, DB, injected env) or release (`APP_VERSION`, compose file in `ci` mode / git branch in `server` mode). Env is merged key-by-key; apply never touches `APP_VERSION`.
**Why:** fixes review N9 (apply resetting the released branch/tag).

### D-033 — A `registry` slot, with GHCR as the golden path · 2026-10-11 · accepted
**Decision:** A `registry` slot holds container registries. `ghcr` is the supported implementation (push with `GITHUB_TOKEN`, pull with a read-only `infra.REGISTRY_PULL_TOKEN`); `dockerhub`, `quay` and generic `oci` are `contrib/`/planned.
**Why:** lets images move between registries without changing app repos.

### D-034 — CI-first: the engine runs in the user's CI; users install nothing · 2026-10-11 · accepted (revises D-031)
**Decision:** The engine image runs in CI jobs (Docker on the runner). Users need a browser, a git hosting account and the prerequisite credentials. An optional local launcher (Homebrew/install script, which runs the pinned image) exists for power users and break-glass. No native toolchain on user machines.
**Why:** The owner wants no local setup; a container keeps tool versions identical everywhere and avoids a macOS/Linux rewrite.
**Rejected:** a native CLI that manages its own tools (portability rewrite); requiring Docker locally as the main path.

### D-035 — Platform-neutral engine with `ci` and `forge` adapter slots · 2026-10-11 · accepted
**Decision:** The engine knows nothing about GitHub or GitLab. `ci` (how jobs run) and `forge` (PR/MR, CI variables, app-repo files, issues) are slots. GitHub ships first; GitLab is designed now and built later.
**Why:** Users may move to GitLab; nothing outside the adapters should change when they do.

### D-036 — Universal infra-repo bootstrap · 2026-10-11 · accepted
**Decision:** Empty repo, plus `sealedstack.version` and the platform's CI snippet, then the *Setup* job proposes `config/` as a PR/MR with a living preflight checklist. Template buttons and local `init` are shortcuts that produce the same files.
**Why:** It works the same on every forge.

### D-037 — PR/MR-only changes; approval-gated apply from `main`; apply never commits · 2026-10-11 · accepted
**Decision:** Setup, Add app and version bumps open PRs/MRs. `main` is protected. Apply runs only from `main` after merge, behind a protected `production` environment. Machine data goes to state, CI variables or the Status issue, never to git.

### D-038 — Pluggable, read-only secret sources; Bitwarden default · 2026-10-11 · accepted (revises D-006 write-back)
**Decision:** `secrets.source` is `bitwarden` (default), `ci`, `sops` or `env`. sealedstack only reads secrets; everything it generates lives in the encrypted state. The tunnel-token write-back to Bitwarden is removed.
**Why:** Any source is easy to support, and a Bitwarden source is portable across forges (CI holds only `BWS_ACCESS_TOKEN`).

### D-039 — Bot identity: a user-owned GitHub App (GitLab: access token); fine-grained PAT as alternative · 2026-10-11 · accepted
**Why:** Short-lived tokens, scoped to chosen repos, not tied to a person, and its PRs trigger plan workflows (the built-in `GITHUB_TOKEN`'s PRs don't).

### D-040 — First server login: root password by default, provider-installed SSH key as alternative · 2026-10-11 · accepted
**Decision:** Used once from CI over public TCP 22. Bootstrap disables password and root login, so the password is useless afterwards.

### D-041 — `tag:ci` may reach `tag:server` on TCP 22 and 3000 over Tailscale · 2026-10-11 · accepted
**Why:** CI bootstraps and syncs servers and calls the Dokploy API. Policy tests enforce that CI can't reach other ports (e.g. 5432) and untagged devices can't reach the panel.

### D-042 — One CLI vocabulary; `just` is contributor-only · 2026-10-11 · accepted
**Decision:** `sealedstack <noun> <verb>` is used by CI jobs, `/sealedstack` comment commands and the local launcher. Preflight (`check`) runs before every command. `up` converges everything. `just` recipes are aliases with no logic.
**Why:** The duplicated just/CLI logic had already drifted (`apply` differed).

### D-043 — The local launcher writes files only; it never commits or pushes · 2026-10-11 · accepted
**Decision:** Repo creation happens only with an explicit flag, using the user's own forge login.

### D-044 — Minimal prerequisites: derive or generate everything else · 2026-10-11 · accepted
**Decision:** R2 S3 keys are derived from the Cloudflare token. Server auth keys are minted by the Tailscale OAuth client. The GitHub App is created by a manifest flow. Generated values are stored in state. The admin SSH public key lives in config, not secrets. The user-created set is: server credential, Cloudflare token, Tailscale OAuth client (+ CI trust), state passphrase, bot identity, secret-source token, plus optional app keys.
**Why:** The number of manual steps is the biggest cost to new users, and every token the user creates is another thing to get wrong.

### D-045 — CI is the only normal writer; local runs are read-only, plus a guarded break-glass mode · 2026-10-11 · accepted
**Decision:** Locally, `check`, `plan`, `status` and `db drill` always work. `apply`, `up`, `server add/sync` and `db restore` are refused unless `--break-glass` is given, and then only when the checkout equals `origin/main`, the state lock is taken, the operator types the infra name, and an audit comment is posted on the Status issue.
**Why:** Two normal writers (CI and laptops) cause state conflicts, drift and unreviewed changes. HCP Terraform blocks CLI apply on VCS-driven workspaces for the same reason.

### D-046 — Local delivery: Codespaces/devcontainer (recommended) and the launcher; no native mode · 2026-10-11 · accepted
**Decision:** Setup generates `.devcontainer/` pointing at the pinned engine image, so local use needs no installs (a Codespace). The launcher plus a Docker-compatible runtime is the alternative. A native toolchain mode is not planned.
**Why:** Local use is optional and read-mostly; a native mode would mean managing OpenTofu, Ansible and Python per OS, and Homebrew dependencies can't be pinned per project.

### D-047 — The engine's orchestration moves from bash to Python · 2026-10-11 · accepted
**Decision:** The CLI and all orchestration are Python, migrated command by command in M6. OpenTofu modules and Ansible roles stay. `gh`, `jq`, `yq`, `curl`, `aws` and `postgresql-client` leave the image as their uses are replaced by API calls and Python libraries.
**Why:** Fragile shell glue caused several review findings (masked secret failures, ineffective caching, deploy polling). Python gives testable code, real error messages and a smaller toolchain (core: OpenTofu, Ansible, SSH).

### D-048 — The user experience is signed off · 2026-10-11 · accepted
**Decision:** [user-experience.md](../user-experience.md) is the final v1 UX: journeys A–H, mockups, command reference, time budgets and the defaults in its §13 (`production` approval environment, hourly status and drift, weekly restore drill on Sunday 03:00 UTC, backup alert at 2× the schedule, version warning after 6 months or on an advisory, `/sealedstack` comment prefix, `sealedstack-bot`, `hello` as the first app). It overrides spec §6 where they differ.
**Why:** It's the contract the implementation is built and accepted against. Changing it needs a new decision.

### D-049 — A server's public IP is a bootstrap secret, never config or a job input · 2026-10-11 · accepted (refines D-008)
**Decision:** For existing machines, the public IP lives in the secret source as `SERVER_IP_<NAME>`, next to `ROOT_PASSWORD_<NAME>` (`<NAME>` = server name upper-cased, `-` → `_`). Both are used once, by the first bootstrap, and can be deleted afterwards. `servers.yaml` holds only name, provider and role. CI jobs take no `--ip` input. Servers that sealedstack creates (API providers) never need it: the IP is learned and kept in encrypted state. Docs use only RFC 5737 documentation addresses.
**Why:** With tunnel-only ingress the origin IP is no longer published in DNS, so keeping it out of git, CI logs and run history removes an easy way to find and target the box directly. It's also used exactly when the root password is, so they belong together.

### D-050 — Secret key names are declared in config · 2026-10-11 · accepted
**Decision:** `stack.yaml: secrets.keys` maps every logical secret to its key in the secret source. Each existing server declares `bootstrap.ip_secret` and `bootstrap.password_secret` in `servers.yaml` (defaults `SERVER_IP_<NAME>`, `ROOT_PASSWORD_<NAME>`). App secrets default to `<app>.<NAME>`, overridable in `apps.yaml`. Setup writes all defaults explicitly; config holds names only, never values.
**Why:** The mapping from setting to secret entry is visible and reviewable in git, users can reuse existing entries without renaming them, and errors can name the exact entry to fix.

### D-051 — One private repo with clean history, under the owner's account · 2026-10-11 · accepted (owner direction; supersedes the mirror plan)
**Decision:** The project is a **single** repository, private at first, under the owner's personal GitHub account (the account link is fine; personal data is not). Development history may be squashed/rewritten so `main` carries only a clean, single-commit history. Personal data lives only in gitignored `internal/`, the owner's private infra/app repos and Bitwarden. Commit/tag identity is the GitHub no-reply address. **Going public** is the owner flipping the repo's visibility once `scripts/publish-check.sh --strict --metadata` passes (content deny-list, personal patterns, gitleaks, and neutral author/committer/tagger identities); a `pre-push` hook blocks non-no-reply identities and non-`main`/`feat/*`/`v*` refs. There is no separate mirror repo.
**Why:** A clean cut-over history plus the metadata/identity checks remove the old-commit and author-metadata leaks by construction, without maintaining a second repository or a publish pipeline.

### D-052 — sealedstack enforces write-command approval (GitHub Free private repos) · 2026-10-11 · accepted
**Decision:** On a private GitHub Free repo, environment required-reviewers and branch protection/rulesets are unavailable, so sealedstack enforces approval itself. `stack.yaml: approvers: [<github-username>, …]` is required whenever `slots.ci` is set, and `setup` defaults it to the repo owner. A **write command** (`apply`, `up`, `restore`, `add-server`, `server sync`) runs in CI only when **both** hold:
1. the commit is on `main` **and** the forge reports it merged through a pull request (a direct push to `main` is refused); and
2. it is approved — the run was dispatched by a listed approver, **or** a listed approver commented `/sealedstack approve` on that PR **after** the latest plan comment.
GitHub environments with required reviewers are still used where the plan supports them (defence in depth, never the only gate). sealedstack's own `v*` release is likewise a **manual workflow dispatch** whose dispatcher must be in the repo variable `RELEASE_APPROVERS` (the `release` environment is a second gate where available).
**Why:** The tool must give private-repo users the same "a human approves writes" guarantee the design promises, on the plan most users are on; and its own images must not publish without an approver.

### D-053 — Nothing is made public until the owner confirms; going public is a late roadmap item · 2026-10-11 · accepted
**Decision:** The repository, container images, Homebrew tap, docs site and any other artifact stay **private** until the owner explicitly confirms otherwise. No workflow, script or agent may change visibility or publish anything publicly. Going public is a roadmap item **after** the live validation (M4) and the owner's own app onboarding. It's gated by the owner's confirmation and a passing `publish-check --strict --metadata` on a repository that was never force-pushed.
**Why:** Publishing can't be undone, so it has to be a deliberate owner decision taken once the project is validated.

### D-054 — R2 S3 credentials are derived from the Cloudflare API token · 2026-10-12 · accepted (implements D-044)
**Decision:** The R2 access key ID is the Cloudflare token's ID (from `/user/tokens/verify`); the secret access key is the SHA-256 hex of the token value. The token must include *Workers R2 Storage: Edit*. `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` are no longer required; they remain an optional override in the secret source.
**Why:** One fewer credential for the user to create and get wrong (D-044).

### D-055 — The admin user has passwordless sudo · 2026-10-12 · accepted
**Decision:** The admin user is granted passwordless sudo through a validated `/etc/sudoers.d/` drop-in. The `admin_sudo_password` secret is removed.
**Why:** Login is SSH-key-only, reachable only over Tailscale, and root and password login are disabled, so a sudo password adds a secret without adding protection.

### D-056 — Callers of the reusable workflows grant `packages: read` · 2026-10-12 · accepted (amends D-048 by one line)
**Decision:** The infra repo's caller workflow grants `packages: read`, so the private engine image can be pulled. The snippets in the user-experience document, the docs and the generated files are updated.
**Why:** The engine image is private (D-053); without the permission the job cannot pull it.

### D-057 — App release jobs use their own Tailscale tag, `tag:release` · 2026-10-13 · accepted (refines D-041)
**Decision:** The reusable app release workflow joins the tailnet as `tag:release`, which may reach `tag:server` on TCP 3000 only (the Dokploy API). `tag:ci` (the infra repo's jobs) keeps TCP 22 and 3000. `tag:release` has its own Tailscale OIDC trust credential, published to app repos as separate variables.
**Why:** App repos should be able to deploy their own releases, but never SSH into servers.

### D-058 — The bot identity for now is a fine-grained personal access token · 2026-10-13 · accepted (defers D-039)
**Decision:** The bot identity (`GH_TOKEN`) is a fine-grained personal access token, limited to the infra repo and the onboarded app repos, with exactly the permissions sealedstack needs. The user-owned GitHub App (D-039) is built later, before going public (a roadmap item next to D-053). The manifest flow stays in the code behind `--experimental-app` and isn't used yet.
**Why:** The App's installation-token minting isn't built; a narrow token is enough for a single owner and doesn't block the live run.

### D-059 — `drift.yml` stays, off by default · 2026-10-13 · accepted
**Decision:** Scheduled drift runs only when the infra repo sets the repository variable `SEALEDSTACK_DRIFT_ENABLED=true`. That one switch gates both the `drift.yml` workflow and the drift step that follows `status` in the hourly scheduled run of `infra.yml`; `status` itself always runs. `drift` is also a manual command (Run workflow and `/sealedstack drift`), which never depends on the switch.
**Why:** Drift checks are useful but not needed for the first live run, and a workflow that fails on repos without config is noise.

### D-060 — Released versions are recorded as GitHub Deployments in the app repo · 2026-10-13 · accepted (refines D-032 and finding F7)
**Decision:** After a successful deploy, the app release workflow records the version as a GitHub Deployment of environment `production` in the app repo: `ref` is the released tag, `payload` is `{"version": "<tag>", "servers": [...]}`, and its status is set to `success` (or `failure` if the deploy fails). It uses the built-in `GITHUB_TOKEN`; the caller grants `deployments: write`. The infra repo reads the latest **successful** `production` deployment of each app repo (the bot token needs *Deployments: read*) and redeploys that version on a freshly created compose, for example after a server rebuild. With no successful deployment there is nothing to redeploy, and the engine says so. The repository variable `SEALEDSTACK_RELEASED_VERSION` is no longer written; it is read only as a deprecated fallback, with a warning. `SEALEDSTACK_BOT_TOKEN` is not used in app repos.
**Why:** The earlier approach wrote a repository variable with a token that is never published to app repos, and the built-in token cannot write variables, so the step failed after a successful deploy. A Deployment is written with the built-in token, keeps a visible history per release in the app repo, and distinguishes a failed release from a good one.

### D-061 — First login is by root password only for now; `bootstrap.method: key` is rejected · 2026-10-30 · accepted (refines D-040)
**Decision:** The first login to a new server uses the provider-issued root password. `bootstrap.method: key` stays in the schema for forward compatibility, but `check` and the cross-file validation reject it with a clear message until it is implemented. A missing root password in CI is a clear error, never an interactive prompt.
**Why:** The key path was accepted but not implemented, so it would hang or fail in CI. Rejecting it early is honest; one working path beats two half-working ones.

### D-062 — The state lock covers CI writes too, carries an expiry, and lives per infra repo · 2026-10-30 · accepted (refines D-045)
**Decision:** The remote state lock is one object in the state bucket at `sealedstack/<infra name>/state.lock`. CI write commands and break-glass runs take the same lock, so they exclude each other. The lock body is `{id, owner, created, expires, run_url}` with a default lifetime of 2 hours; an expired lock may be taken over (with a warning naming the previous holder), and the Status issue reports a lock held longer than 1 hour. Release removes only the caller's own lock.
**Why:** A lock that only break-glass takes does not protect against a CI run, and a runner killed mid-apply must not block the stack forever.

### D-063 — App repos get a scoped Dokploy member key, never the admin key; the release job lints the compose file · 2026-10-30 · accepted (refines D-057)
**Decision:** Each app repo is given a Dokploy API key of a **member created for that app** and limited to that app's project, never the admin key. Defence in depth: the release job and the infra `check`/`apply` lint the rendered deploy compose and refuse host-escape settings (privileged containers, the Docker socket or host paths outside a short allow-list, host network/pid/ipc namespaces, extra capabilities, devices, disabled security confinement).
**Why:** The Dokploy API controls every app and, through compose, the host. A writer of one app repo must not gain that.

### D-064 — The first SSH connection to a new server can pin the host key · 2026-10-30 · accepted
**Decision:** `servers.yaml: <name>.bootstrap.host_key_fingerprint` (SHA256 form, as providers show it) is optional. When set, the bootstrap play runs with host-key checking enabled against a generated `known_hosts` holding the key whose fingerprint matches; a mismatch is refused. When absent, behaviour is unchanged and `check` reports it as a recommendation (`info`, shown as ◻): it names where to find the fingerprint, exits 0 and never blocks the Setup checklist. *Amended 2026-10-31: absent = recommended, non-blocking in the checklist.*
**Why:** The first connection sends the root password over a path nothing authenticates yet; a pinned fingerprint closes that gap for users who take the extra minute.

### D-065 — App repos hold no Dokploy credential; the infra repo performs deploys · 2026-10-31 · planned (closes review finding FR-1; builds on D-063)
**Decision:** App repos will hold **no** Dokploy credential. The infra repo performs every deploy. The app release job only records the release (a GitHub Deployment, already done, D-060) and requests it, for example by a `repository_dispatch` to the infra repo, or the infra repo picks it up on its schedule, or an approved `/sealedstack release <app> <tag>` command. The infra CI (`tag:ci`) then pulls the rendered compose from the app repo at that tag, lints it (D-063), and deploys it. **To be built after M4 and before anyone else gets write access to an app repo, or the project goes public.**
**Why:** A Dokploy key that can deploy an app can also edit that app's compose and command, and a compose file is root on the host (review FR-1). The scoped member key (D-063) limits the blast radius to one app's server but does not close it, because Dokploy has no "deploy but do not change" permission. Taking the credential out of app repos removes the path instead of guarding it.

### D-066 — Doppler is a native, read-only secret source · 2026-10-31 · accepted (adds an alternative to D-006; refines D-038)
**Decision:** `slots.secrets: doppler` reads sealedstack's secrets from one Doppler config over Doppler's REST API (`GET /v3/configs/config/secrets/download?format=json`), authenticated by a **service token** (`dp.st.…`), which Doppler scopes to a single config and makes read-only by default. `DOPPLER_TOKEN` reads the infra secrets; an optional `DOPPLER_TOKEN_APPS` reads app secrets from a second config, falling back to the first. Other token types are refused. One request serves every lookup in a run, rate limits (429) and 5xx are retried, and the token never appears in output. Doppler names allow only `A-Z`, `0-9` and `_`, so a key is looked up as written, then upper-cased with other characters replaced by `_` (`shop.CLERK_SECRET_KEY` becomes `SHOP_CLERK_SECRET_KEY`). The reusable workflow forwards `DOPPLER_TOKEN` and `DOPPLER_TOKEN_APPS` by name, like `BWS_ACCESS_TOKEN`, and never the whole secrets bundle. The earlier Doppler adapter ran the `doppler` CLI, which the engine image does not contain, so it could not work in CI; it is removed.
**Also:** the edge stack no longer requires `BWS_ACCESS_TOKEN` (its `bws_access_token` variable was dead since the Bitwarden write-back was removed, D-038), which had blocked every non-Bitwarden source from `apply`.
**Why:** The owner wanted alternatives to Bitwarden. Doppler's service token fits the principle that the single secret CI holds can read exactly one config and write nothing, and a REST call needs no extra tool in the image (D-047).
**Rejected:** keeping the CLI adapter (needs a binary in the image); personal or service-account tokens (not scoped to one config); a configurable API base URL (a pull request could point the token at another host).
**Open:** not run against a live Doppler account; the docs are inconsistent about whether a service token needs `project` and `config` parameters (they say not), so the first live run confirms the bare call works.

### D-067 — Infisical is a native, read-only secret source · 2026-10-31 · accepted (adds an alternative to D-006; refines D-038; follows D-066)
**Decision:** `slots.secrets: infisical` reads sealedstack's secrets from one folder of one Infisical project environment over Infisical's REST API (`GET /api/v4/secrets`, one request per run). It authenticates with a **machine identity**: Universal Auth (`INFISICAL_CLIENT_ID` + `INFISICAL_CLIENT_SECRET`, exchanged at `POST /api/v1/auth/universal-auth/login` for a short-lived token each run, the recommended method) or a Token Auth access token (`INFISICAL_TOKEN`, used as is, wins if set). Where the secrets live is plain config, `providers.secrets_infisical` (`project_id` required, `environment` default `prod`, `secret_path` default `/`, `apps_secret_path` for app secrets, `site_url` default `https://app.infisical.com`, https only so EU and self-hosted instances work; plain http is accepted only on loopback for tests). Rate limits (429) and 5xx are retried, a value hidden from the identity is an error naming the missing permission, and no credential appears in output. Names are tried as written, then upper-cased with other characters replaced by `_`. The reusable workflow and `drift.yml` forward the three variables by name and never the whole secrets bundle. The earlier adapter ran the `infisical` CLI, which the engine image does not contain, so it could not work in CI; it is removed. Setup accepts `--secrets infisical` and writes the provider block with an empty `project_id`, which `check` reports until it is filled in.
**Why:** The owner wanted alternatives to Bitwarden. A machine identity whose client secret is exchanged per run leaves nothing in GitHub that expires into a stale token, it is limited to the roles given to it, and a REST call needs no extra tool in the image (D-047).
**Rejected:** keeping the CLI adapter (needs a binary in the image); a second identity for app secrets (a folder plus a role restriction covers it for now); recursive reads and secret imports (surprising scope for a read of "the infra secrets").
**Open:** not run against a live Infisical account. The v4 query parameters, Infisical's secret-name rules and whether Token Auth tokens are accepted as a plain Bearer come from the documentation only, so the first live run confirms them.

### D-068 — Setup writes an AGENTS.md into the infra repo · 2026-10-31 · accepted (extends D-046; follows D-052)
**Decision:** Setup adds `AGENTS.md` to PR #1, and a one-line `CLAUDE.md` (`@AGENTS.md`) so Claude Code reads the same file. It states the rules for an AI coding agent in the infra repo: change things only through pull requests, never apply or use `--break-glass`, never see secret values (`config/` holds names only, and the file tells the agent to ask the owner for the exact key instead), the owner merges PR #1 and approves `up`, no creating accounts or tokens, one change at a time when swapping a slot. It also names the secret source's credentials for the chosen source, the slash commands and workflow dispatches that exist, and where the documentation is. The file holds no secret, address or personal value, and editing it never triggers an `apply` (it is outside the relevant prefixes of `infraci`).
**Why:** Users will set sealedstack up with an agent. The guidance should travel with the repository instead of depending on the user pasting a prompt, and the approval gate (D-052) already stops an agent from applying, so the file mainly tells it how to be useful inside that limit.
**Rejected:** a long tutorial in the file (the full guide stays in the sealedstack docs, linked); one file per agent product (`AGENTS.md` plus a pointer covers the common ones).

### D-069 — Documentation is split by audience · 2026-10-31 · accepted (refines D-068)
**Decision:** `docs/` holds only what someone **using** sealedstack (and an AI agent helping them) needs: the guides, the catalog, the limitations and the user-experience reference. Everything about how sealedstack is built and why moved to `docs/maintainers/`: the architecture, the design spec, this decision log, the development notes, and the recipe for adding a provider (split out of `providers.md`). The root `AGENTS.md`, `CLAUDE.md` and `CONTRIBUTING.md` are for maintainers and say so at the top; each maintainer page opens with a banner saying it is not setup guidance and may describe superseded designs. The `AGENTS.md` that Setup writes into an infra repo lists the user documents in reading order and tells the agent to ignore `docs/maintainers/` and the sealedstack repo's own agent files. The documentation site shows maintainers' pages in their own section. User pages may still cite a decision (`D-0nn`), which links into the log, but they do not tell the reader to follow a maintainer page.
**Why:** An agent that reads the sealedstack repository to help a user could land on the design spec (which still describes the older laptop flow), the decision log (which keeps superseded entries) or the root `AGENTS.md` (which tells contributors to run `just`) and follow the wrong one. A physical split with explicit banners, plus tests, keeps the paths apart.
**Rejected:** labelling only (nothing stops a link or a search from landing on the wrong page); deleting superseded material (the log keeps history on purpose); moving the user-experience reference to maintainers (users and engine messages point at it).

### D-070 — `reinstall` wipes and reinstalls a Netcup server from CI · 2026-10-31 · accepted (extends D-052, D-061; follows D-049)
**Decision:** `sealedstack reinstall --name <server> --confirm <server>` (canonical `server reinstall`) calls Netcup's SCP REST API to format the server's disk and install an image (`POST /servers/{id}/image`), waits for the returned task, removes the old `tag:server` node from the tailnet and clears the saved `up` progress. It is a write command: the engine's approval gate (D-052) applies, it is dispatched from Actions only (never by a comment, because it needs typed input), `--confirm` must repeat the name, `--dry-run` prints the server, disk and image that would be erased, and a local session needs `--break-glass`. Authentication is Netcup's OpenID Connect realm: the offline **refresh token** (`NETCUP_REFRESH_TOKEN`, key renamable through `secrets.keys.netcup_refresh_token`) is exchanged at the token endpoint for each run; the one-time device-flow login that produces it is documented, not automated. The root password is **chosen by the user** and read from `ROOT_PASSWORD_<NAME>` (min. 12 printable characters, no spaces), because the secret source is read-only (D-038); it reaches the new install through a first-boot script (base64 inside the request, never in logs) that sets it and allows root password login for the bootstrap only, which the baseline turns off again (D-061). The server is found through `external_id` (id or `v22…` name), the image from `os` (default `ubuntu-24.04`, exact alias or a single name match, else the choices are listed and `--flavour <id>` decides), the disk is the server's only one or `--disk`.
**Why:** The owner wanted a from-nothing test without clicking in the provider panel, and the SCP API offers the call. A rebuild is exactly the case the rest of the flow (bootstrap by root password, state in R2, everything else from git) was designed for, so only the wipe was missing.
**Rejected:** generating the password inside the command (nowhere to store it: no write-back); a Netcup username and password login (the realm's password grant would put the account password in CI, a refresh token is revocable and scoped to the API); letting `up` call `reinstall` when a server looks unhealthy (an erase must be a deliberate, typed act); comment-triggered `reinstall` (no typed confirmation); creating servers (Netcup's API does not order them).
**Open:** not run against a live account (limitations). Unverified: the refresh-token exchange for the `scp` client from CI, that the first-boot script runs as root before SSH is used, how Netcup handles `diskName` for a disk named other than the reported `dev`, and whether a stored Dokploy key in the state is detected as stale on the next `up` (the bootstrap code says it is). The refresh token lapses after about 30 days unused, and sealedstack cannot renew it.

### D-071 — A maintainer can run the engine from a checkout against a real infra repo · 2026-11-01 · accepted (supplements D-045: CI stays the only normal writer)
**Decision:** `scripts/dev-local.sh` runs the engine from the working tree inside the toolbox image, with the infra repo mounted at `/infra` and only the secret-source credentials and the bot token passed in from an env file (by name; the file is never sourced). `--tailnet` joins the tailnet from inside the container as an ephemeral `tag:ci` node (`scripts/dev-tailnet-entry.sh`), because a Docker Desktop container cannot use the host's Tailscale. The CLI mode keeps every guard. `--phase` runs a module, a maintainers' script, or the whole `up`/`apply`/`plan` sequence directly, which skips the approval gate, the state lock and the audit post; it is for rebuildable test setups. `scripts/policy-validate.py` asks Tailscale to validate the merged policy without applying it.
**Why:** The first live `up` found one bug per release cycle (about ten minutes each): no `-auto-approve`, R2 has no versioning, no `sshpass` in the toolbox, a tunnel ingress shape the provider rejected, policy tests in the wrong shape. A local loop finds them in about a minute each. The final check is still one clean run through the pipeline, because the local loop cannot show CI-only behaviour (the lock, the approval, the OIDC join, comments).
**Rejected:** relaxing the guards for local runs (the point of D-045); installing OpenTofu and Ansible natively (the toolbox image is the tested environment).

### D-072 — The CI tailnet join uses its own Tailscale OAuth client · 2026-11-01 · accepted (refines D-058, D-057; found in the first live run)
**Decision:** The OAuth client in `tailscale_oauth_client_id/_secret` is for the engine: policy, devices and `tag:server` keys, with the tag `tag:server` only. The CI fallback join (no workload-identity trust credential configured) reads `tailscale_ci_oauth_client_id/_secret` (default keys `TAILSCALE_CI_OAUTH_CLIENT_ID/_SECRET`), a second client with the tag `tag:ci` only, and falls back to the first pair when the second is absent. The workflow exports the CI pair; `scripts/dev-local.sh --tailnet` uses it too.
**Why:** Verified against the live API: a client may mint a key only for exactly its own tag set. With tags `tag:ci` and `tag:server` the pair is accepted and each tag alone is refused; a client with only `tag:ci` cannot make a `tag:server` key. One client therefore cannot both bootstrap servers and join CI, which the earlier fallback assumed.
**Rejected:** a key carrying both tags (a server would gain `tag:ci` rights); minting `tag:ci` keys from the server client through tag ownership (not how the API behaves).
**Open:** the Setup checklist and `check` still only test that the client can mint a token; they should try a key for each tag sealedstack needs.

### D-073 — The engine owns the tunnel connector's token and deploy · 2026-11-01 · accepted (applies D-032 to the connector; found in the first live run)
**Decision:** After every `platform apply` the engine's `connector` phase makes each `<app>-connector` compose carry its token (`TUNNEL_TOKEN`, or `NGROK_AUTHTOKEN`), keeping every other env line. It asks Cloudflare whether the tunnel has a live connection. A connected tunnel is never restarted (restoring the env alone is enough); an unconnected one is deployed once and then watched for up to two minutes, and the phase fails with the likely cause (the provider firewall must let Cloudflare's replies reach UDP source port 7844) when it still does not connect. The platform module no longer has a `dokploy_environment_variables` resource or a `connector_token` variable for it. `dokploy_compose.connector` has `lifecycle { ignore_changes = [env] }`: without it every apply saw the engine's token as drift, set `env` back to null and redeployed the container with a blank token (found by re-running `up` locally; the run after the fix reports "No changes").
**Why:** The provider's `dokploy_compose` shows an in-place change on every apply and its update clears the compose `env`, while `dokploy_environment_variables` believes its value is still set. Every deployment therefore started `cloudflared` with a blank token and Cloudflare answered error 1033. Two writers for one field lost the value; one writer that also checks the outcome does not.
**Rejected:** `ignore_changes` on the compose (blocks compose-file changes such as an image bump); redeploying the connector on every apply (a brief outage each time).

### D-074 — The Netcup provider firewall is managed through the SCP API · 2026-11-01 · accepted (replaces the placeholder REST adapter for Netcup; follows D-070)
**Decision:** `sealedstack.netcupfw` keeps one user policy named `sealedstack` holding the rule table of `docs/netcup-firewall.md` and attaches only it (plus Netcup's own copied policies) to the server's interface through `PUT /servers/{id}/interfaces/{mac}/firewall`. Policies the operator made are detached, not deleted, and the plan names them. The one moving rule is TCP 22: `up` and `reinstall` open it before a first login, `up` closes it after the baseline. `--dry-run` shows the plan and writes nothing. The SCP user id is the `id` claim of the access token. Without `NETCUP_REFRESH_TOKEN` or `external_id`, `up` keeps the manual steps.
**Why:** The first live run had a hand-made policy that lacked the `UDP src 7844` reply rule (cloudflared could not reach Cloudflare) and still opened TCP 80 and 443 at the provider. Rules applied by hand drift; the rule table should be applied the same way every time, and a temporary rule should close itself.
**Rejected:** deleting the operator's own policies (an unrequested destructive change); leaving 22 open for convenience.
**Open:** unverified live until the first apply (the policy create/update and interface PUT shapes come from Netcup's API description).

### D-075 — Tailscale workload-identity (OIDC) for the CI join is parked · 2026-11-01 · deferred
**Decision:** Not now. The workflow already supports an OIDC trust credential for the CI join (`providers.mesh_tailscale.oauth_client_id` and `audience`), which would remove the second stored OAuth client (D-072) and leave one stored Tailscale secret (the engine's `tag:server` client). Revisit after the first end-to-end pipeline run is clean.

### D-076 — The Dokploy admin password can come from the secret source · 2026-11-01 · accepted (refines D-038, D-044; keeps the secret source read-only)
**Decision:** If `DOKPLOY_ADMIN_PASSWORD` (logical name `dokploy_admin_password`, at least 12 characters) is in the secret source, the `dokploy-bootstrap` phase applies it: a fresh panel gets its admin created with it; an existing admin is signed in with the generated password from the encrypted state and moved to the chosen one through `POST /api/auth/change-password`, then checked by signing in again; a password that already works is left alone; an admin whose password is neither is reported with the fix. The chosen password then wins over the generated one for everything the engine does as the admin (creating the API key). `status --show-dokploy-admin` points at the secret source instead of revealing anything, and does not use up the one-time reveal. Without the secret nothing changes: generated, kept in the state, shown once.
**Why:** The generated password was shown once and then gone, which is a trap for an operator who missed it. A value the operator chooses and keeps with their other secrets needs no write access to the secret source: it flows one way, from the secret source into Dokploy, which keeps CI's token read-only.
**Rejected:** writing the generated password back into the secret source (needs a write token in CI, D-038); allowing a second reveal (it prints the secret again and still leaves nothing durable).
**Open:** the change-password call is exercised against a fake panel and then once against the live panel from a maintainer's machine; Dokploy's behaviour on v0.30.8 is documented nowhere we could find.

### D-077 — The root password for the provider console is applied from the secret source · 2026-11-01 · **withdrawn 2026-11-01** (released as 0.6.11, removed in 0.6.12; follows D-061, D-076)
**Withdrawn:** the owner will handle root password rotation another way, so the feature, its role task and its tests were removed. The text below stays as the record of what was tried. The bootstrap password (`ROOT_PASSWORD_<NAME>`) is still a one-time value: delete it, and the server IP, from the secret source after the first `up`, and add a new one before the next `reinstall`.

**Decision:** If `CONSOLE_PASSWORD_<NAME>` (renamable with `bootstrap.console_password_secret`) is in the secret source, the baseline (`base` role, run by `up` and `server sync`) sets it as root's password with `chpasswd`, fed on stdin and never logged, and records the SHA-256 of the applied value in a root-only marker so a re-run changes nothing until the value changes. The value must be at least 12 characters on one line. It is passed to the baseline only, never to the bootstrap play, which logs in with the one-time bootstrap password. SSH as root and password logins stay off (D-061); this decides only what the provider console accepts. The one-time bootstrap password (`ROOT_PASSWORD_<NAME>`) and the server IP can then be deleted from the secret source. Absent: the root password is left as the install set it.
**Why:** The bootstrap password sits in the secret source for the whole first run and works at the console afterwards. The owner wanted it rotated after configuration. A value the operator chooses and rotates by editing the secret source needs no write access to it.
**Rejected:** writing a generated password back to the secret source (a write token in CI, D-038); locking root (no console login at all: the admin user has no password, so a broken tailnet would leave only the provider's rescue system); a generated password shown once (the trap D-076 removed for Dokploy).


### D-078 — Security response headers are set on the app hostnames at the Cloudflare edge · 2026-11-01 · accepted
**Decision:** The edge stack adds one zone ruleset, `sealedstack-security-headers` (phase `http_response_headers_transform`), that sets `Permissions-Policy` (camera, microphone, geolocation, payment, USB and interest-cohort off), `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin` and `Strict-Transport-Security: max-age=31536000` (no includeSubDomains, no preload) on every hostname of every deployable app. It is on by default and turned off with `edge_security.security_headers: false`. The Cloudflare token needs `Zone · Transform Rules: Edit`; `check` probes it.
**Why:** A header scan of a gated app showed `Permissions-Policy` missing. The `unsafe-inline` CSP warning in the same scan belongs to Cloudflare's Access login page on the team domain, which a scanner reaches by following the redirect; it cannot be changed from the zone and is not ours to fix.
**Rejected:** a default `Content-Security-Policy` (a policy that suits one app breaks another's inline scripts, CDNs or frames; apps send their own); `X-Frame-Options` (same reason, embedding is a legitimate use); HSTS (Cloudflare has its own zone setting for it).
**Open:** whether the Access 302 itself carries the headers is checked against the live zone after the first apply.

### D-079 — The Cloudflare zone accepts TLS 1.2 and newer; AEAD-only ciphers are opt-in · 2026-11-01 · accepted (ciphers made opt-in in 0.6.18)
**Decision:** The edge stack manages two zone settings: `min_tls_version` (default `1.2`, `edge_security.min_tls_version`) and, only when `edge_security.restrict_tls_ciphers` is true (default **false**), `ciphers` (six ECDHE suites with AES-GCM or ChaCha20). TLS 1.3 suites are not affected. The Cloudflare token needs `Zone · Zone Settings: Edit`; `check` probes it.
**Why:** Qualys SSL Labs scans of the gated hostname capped the grade at B because the zone still accepted TLS 1.0 and 1.1, and flagged `TLS_ECDHE_ECDSA_WITH_AES_128_CBC_SHA` as weak. Both come from Cloudflare's zone defaults, which sealedstack never set.
**Rejected:** HSTS as a zone setting (its provider schema is a nested object we could not verify offline; the response-header rule from D-078 carries it, scoped to the app hostnames); `includeSubDomains` and `preload` (hard to undo, and the zone has other subdomains).
**Open:** clients older than TLS 1.2 (Android 4.x, IE 10 and older) can no longer connect; `min_tls_version: "1.0"` brings them back. Whether the Access 302 carries HSTS is checked after the first apply. Cloudflare's own Access login page keeps its own TLS and headers.

### D-080 — Releases are cut by a script, not by hand · 2026-11-01 · accepted
**Decision:** `scripts/release.sh` cuts a release from a rebased checkout: it takes one more than the highest of `VERSION` on `origin/main` and every `vX.Y.Z` tag, commits `release: X.Y.Z`, runs the tests, `tofu fmt -check` and `publish-check --strict`, refuses if `origin/main` moved meanwhile, then pushes `main` and the lightweight tag and dispatches the release workflow and waits for it. `--dry-run` shows the version and the commits that would ship. The CLI test reads `VERSION` instead of hard-coding the number.
**Why:** Two sessions released in parallel and each picked a number the other had already used, which meant a rebase and a re-bump by hand each time. The hard-coded version in `test_engine_cli.py` was the line that conflicted.
**Rejected:** bumping inside the release workflow (the version belongs in the commit that ships, and the tests must see it); a release on every merge (releases stay a deliberate maintainer action, D-052).
**Amended 2026-11-01 (0.6.18):** the first live apply of 0.6.17 failed on the `ciphers` setting: `PATCH .../settings/ciphers` answered 400, code 1023, "Advanced Certificate Manager is required to set custom cipher suites". It stopped the edge apply, which blocked the platform step after it. Custom ciphers therefore default to off. The grade cap in the scans came from TLS 1.0/1.1, which `min_tls_version` fixes on every plan; the CBC-SHA suite is a warning that Cloudflare's default cipher list keeps, and only a zone with Advanced Certificate Manager can remove it. **Rejected:** detecting the add-on from the API (no stable read for it on a free zone), and keeping the default on with a clearer error (a failing default blocks the whole apply).

### D-081 — Private images need a registry pull token, and `check` probes it · 2026-11-01 · accepted (refines D-033)
**Decision:** When an app has its own repository (images built in CI and pushed to a private package), `check` reports a `registry.pull_token` row. It fails when `REGISTRY_PULL_TOKEN` (or the key mapped in `secrets.keys.registry_pull_token`) is missing, and probes a present token against GitHub: it must be a classic token whose `X-OAuth-Scopes` include a package scope. The registry owner defaults to the GitHub owner of the first app repository, so `providers.registry_ghcr.owner` is only needed to override it. `edge plan` now also writes `.build/edge-outputs.json` from the state, so a PR plan sees the backup endpoint.
**Why:** The first live release failed with `unauthorized` on every image. The token was not a prerequisite, nothing checked it, and the first token tried was fine-grained: GitHub accepts it, ghcr.io answers 403 for every package. Separately, PR plans after the first database app listed the backup destination, panel backup and database backup as destroyed, because only `edge apply` wrote the outputs the platform stack reads for the backup endpoint.
**Rejected:** reusing the bot's `GH_TOKEN` (fine-grained, so ghcr.io refuses it, and far broader than a pull credential); making the packages public (valid for demos, so it stays documented as the no-token option).
**Open:** a classic token reads every package of the account, so a dedicated machine account is better for anything beyond a personal setup. Two apps that publish the same service name on `dokploy-network` of one server can resolve each other's containers; `check` warns (`config.compose`) and the names have to be changed by hand.

### D-082 — A release waits for a green `ci` run on its commit · 2026-10-31 · accepted (refines D-052, D-080)
**Decision:** The release workflow's first job after `authorize` is `ci-gate`. It asks the Actions API for the newest `ci` run whose `head_sha` is the dispatched commit and waits (up to 30 minutes) until that run has completed. Only `success` lets the release go on; a failed, cancelled or skipped run, a missing run (after 5 minutes), or an API error stops it before the toolbox is built. The `toolbox` and `release` jobs depend on the gate, and `release` no longer starts with a bare `always()`, so a failed gate or toolbox build cannot be skipped over. The dry run is not gated: it pushes nothing. `scripts/release.sh` itself waits for that `ci` run (on the maintainer's machine, so it costs no runner time) and only then tags and dispatches, so the gate normally finds a finished run and takes seconds; a red run stops the script before the tag.
**Why:** Release 0.6.15 went out with a failed `ci` run. `scripts/release.sh` runs the tests and `tofu fmt -check` locally, but nothing tied the release to what CI said about the exact commit. The gate waits instead of only checking because the script dispatches the release right after pushing, while `ci` is still running.
**Rejected:** a `workflow_run` trigger (a release stays a manual dispatch, D-052); an input that skips the gate (re-run `ci` instead); requiring `ci` through branch protection (not available on a private Free repository).
**Open:** the gate is exercised against a stub `gh` in `tests/pytest/test_engine_release.py`; only a real release run shows it against the live API.

### D-083 — CI is one job graph that resolves the toolbox once · 2026-10-31 · accepted (refines D-053, D-082)
**Decision:** `ci.yml` is the only pull-request pipeline. A first job, `prepare`, works out what changed and finds the published toolbox image for this exact `toolbox/` content (the `tree-<git tree hash>` tag that `toolbox.yml` writes), read-only. The jobs that need the toolbox (`lint`, `tests`, `tofu`) pull that digest through the local action `.github/actions/toolbox` and run commands with `in-toolbox`; none rebuilds it. If no image exists for the current `toolbox/` (a change that touches it, not yet merged), `prepare` builds it once into the Actions layer cache and the action imports that. The checks run in parallel:
- `lint`: config render, registry check, `validate.py`, ansible-lint and syntax-check, shellcheck, publication hygiene and a full-history gitleaks scan (`scripts/gitleaks-history.sh`, which fails when the scan could not read the repository).
- `tests`: pytest with `-n auto` (pytest-xdist is in the toolbox).
- `tofu`: `fmt` and `validate` inside the toolbox (one OpenTofu pin, the toolbox's), with a provider plugin cache keyed on the lock files; the five `init`s run in turn and the five `validate`s side by side (`scripts/tofu-validate.sh`).
- `docs`: calls `docs.yml`, which builds the site strict and, on main, uploads that same build as the Pages artifact for its gated deploy. `docs.yml` no longer has a `push` trigger. Both D-053 gates are unchanged, and the deploy never runs for a pull request.
- the Docker checks are steps of `lint`, run only when something they read changed: the tool image build (F2), then the reference app, built once, run for `/api/health`, and started with `docker compose up --no-build` on the same image. Editing a file under a path the Dockerfile copies cannot break it; deleting or renaming one runs the build. The reference app's pytest files, `registry.py check` and `validate.py` were already covered by `tests` and `lint`, so `reference-app.yml` is gone.
Docs-only changes skip `tofu` and the Docker checks; changes outside the paths a job reads skip it. A pull request cancels its own older run; a push to main never cancels or is cancelled.
**Why:** The measured run did the same work in several places: the toolbox was built three times (buildx setup and image load in two jobs), `reference-app` built its image twice and re-ran three pytest files, `tofu validate` downloaded every provider five times, and about 73% of pytest time was `shutdown()` waiting on the default 0.5 s poll of the fake servers. A `gitleaks` job that scanned nothing is fixed in the same change. Runner time is billed per job, rounded up to a whole minute, so the graph avoids small jobs: gitleaks and the Docker checks are steps of `lint`, and the change detection and the toolbox lookup are one job. A typical change (no Docker paths) bills prepare, lint, tests and tofu: four minutes, against nine before.
**Rejected:** calling `toolbox.yml` from `ci` (it builds and pushes when the tag is missing, so a pull request could publish an image to GHCR, against D-053; `ci` only reads the tag); skipping pytest on docs-only commits (14 test files read `docs/` and pin documentation to behaviour, so a docs edit can break them); `container:` jobs (they cannot fall back to a local build); pushing candidate tool images from `ci` (D-053).
**Open:** nothing here could run on GitHub from a worktree: the GHCR pull with `GITHUB_TOKEN`, the layer-cache fallback, the plugin cache with the committed lock files, and calling `docs.yml` with the Pages permissions from a pull request are checked by the first real run. Native arm64 toolbox builds (instead of QEMU) are not done: private Free repositories have no arm runners.

### D-084 — Restore drills are levelled, isolated and opt-in · 2026-11-02 · accepted (replaces the first drill of D-047)
**Decision:** `drill` restores the newest backup into a **separate throwaway Postgres container** on the app's server (no network, a memory limit, `--rm`), never into the live instance. It has five levels, each including the ones before: `verify` (gzip checksum over the whole file and the archive contents), `schema` (the structure applies), `sample` (the first rows of every table, constraints and triggers off), `tables` (the full data of the tables the app names, counted against `min_rows`; the default) and `full`. Data levels check the free disk first (`min_free_ratio` times the dump, or the live database for `full`) and stop with a message instead of restoring. The report lists what was tested and what was not. Settings: `drill:` in `stack.yaml` (`scheduled`, `level`, `min_free_ratio`, `memory`) and per app in `apps.yaml` (`enabled`, `level`, `tables`, `min_rows`, `sample_rows`). The weekly cron is in the workflow template, and `drill.scheduled` (off by default) decides whether it does anything; running by hand always works. `drill all` covers every app with a managed database except those with `drill.enabled: false`.
**Why:** The first drill created a database inside the live Postgres on the same server, with no space check: a drill on a large database could fill the disk the app runs on. It also could not say what it had and had not proven, and it could not find Dokploy's backups (D-081 era fixes). A one-row sample cannot prove constraints (a sampled child row points at a parent that was not sampled), triggers, or disk and time, so those claims are made only by the levels that really test them.
**Rejected:** running the restore on the CI runner by default (the production data would leave the server for a GitHub-hosted VM; it stays a possible follow-up for users who accept that); restoring a subset of rows (pg_restore restores whole tables only; filtering the COPY stream is used for `sample` and is labelled as a sample).
**Open:** the dump is held in memory and sent over SSH (limit 2 GiB), so very large backups need a streaming path; plain Postgres images lack extensions such as PostGIS, which then fails at `schema`.

