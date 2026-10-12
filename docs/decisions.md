# Decision Log

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
**Why:** It fixes review N9, where `just apply` reset the tag to `main`, and it keeps D-015.

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
**Decision:** [user-experience.md](user-experience.md) is the final v1 UX: journeys A–H, mockups, command reference, time budgets and the defaults in its §13 (`production` approval environment, hourly status and drift, weekly restore drill on Sunday 03:00 UTC, backup alert at 2× the schedule, version warning after 6 months or on an advisory, `/sealedstack` comment prefix, `sealedstack-bot`, `hello` as the first app). It overrides spec §6 where they differ.
**Why:** It's the contract the implementation is built and accepted against. Changing it needs a new decision.

### D-049 — A server's public IP is a bootstrap secret, never config or a job input · 2026-10-11 · accepted (refines D-008)
**Decision:** For existing machines, the public IP lives in the secret source as `SERVER_IP_<NAME>`, next to `ROOT_PASSWORD_<NAME>` (`<NAME>` = server name upper-cased, `-` → `_`). Both are used once, by the first bootstrap, and can be deleted afterwards. `servers.yaml` holds only name, provider and role. CI jobs take no `--ip` input. Servers that sealedstack creates (API providers) never need it: the IP is learned and kept in encrypted state. Docs use only RFC 5737 documentation addresses.
**Why:** With tunnel-only ingress the origin IP is no longer published in DNS, so keeping it out of git, CI logs and run history removes an easy way to find and target the box directly. It's also used exactly when the root password is, so they belong together.

### D-050 — Secret key names are declared in config · 2026-10-11 · accepted
**Decision:** `stack.yaml: secrets.keys` maps every logical secret to its key in the secret source. Each existing server declares `bootstrap.ip_secret` and `bootstrap.password_secret` in `servers.yaml` (defaults `SERVER_IP_<NAME>`, `ROOT_PASSWORD_<NAME>`). App secrets default to `<app>.<NAME>`, overridable in `apps.yaml`. Setup writes all defaults explicitly; config holds names only, never values.
**Why:** The mapping from setting to secret entry is visible and reviewable in git, users can reuse existing entries without renaming them, and errors can name the exact entry to fix.

### D-051 — The development repo stays private; the public project is a cleaned release mirror · 2026-10-11 · accepted
**Decision:** Development happens in a private repo whose history is never published. Each public release is produced by a publish workflow that exports an allow-listed tree at the release tag, runs the publish checks (deny-list, IP/MAC/email/ID patterns, gitleaks, metadata), and commits it to the public repository as a single release commit under a neutral identity in a GitHub organization. The first publish is a dry run reviewed and approved by the owner.
**Why:** It guarantees that no personal data in old commits, author metadata or private notes can reach the public project, by construction rather than by a one-time cleanup.
