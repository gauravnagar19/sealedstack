# sealedstack — Design Spec

| | |
|---|---|
| Status | Draft for review |
| Date | 2026-10-11 |
| Author | sealedstack maintainers |
| Decisions | [decisions.md](decisions.md) |
| Future work | roadmap |

---

> **Update (2026-10-11):** distribution and releases changed — see
> [decisions.md](decisions.md) (D-022..D-033). It
> supersedes this document for: repos and layout (§3), the `registry` slot and
> provider maturity (§4), the app manifest `build` block (§5.4), Tailscale policy
> management (§8), the Dokploy ownership/env contract (§9), commands (§10, now a CLI),
> the delivery plan (§15, milestones M0–M5) and risks (§16, R8–R10).
>
> **Update (CI-first):** the signed-off [user experience](user-experience.md)
> (D-034 to D-053) wins wherever it differs from this document. The engine runs in
> the user's own CI from a pinned container image, users install nothing, and the
> only commands users see are `sealedstack <noun> <verb>` (§10, §14). `just` is a
> contributor convenience only.

## 1. What it is

sealedstack takes **an empty Linux box you can SSH into** and turns it into a **sealed origin** that runs your apps. It is generic across providers, domains and apps.

A sealed origin works like this:

- **Zero inbound ports from the internet.** The provider firewall, UFW and Docker's `DOCKER-USER` chain all drop everything.
- **Admins reach the box only over a private mesh** (Tailscale).
- **Users reach apps only through an outbound tunnel** (Cloudflare Tunnel). Bots are filtered, and internal hostnames require a login (Cloudflare Access), all before any traffic touches the server.
- **Everything can be rebuilt from three sources:** git (config), the secrets provider (credentials) and object storage (data backups).

### Goals

1. **Empty box → hello app live behind Access in ≤ 15 minutes** using one command after prerequisites are in place.
2. **The same app runs on a different server** after changing one line in config.
3. **Free or near-free:** every service used has a free tier that covers a solo operator.
4. **Generic by contract:** every external dependency sits behind a *slot* (§4). The MVP ships one implementation per slot.
5. **Open-source ready:** the generic code never contains user-specific values. Those live in `config/` only.

### Non-goals

These are permanent. Deferred work is listed in roadmap.

- Kubernetes.
- Writing our own auth, secrets store, tunnel or PaaS.
- Multi-tenant hosting for third parties.

---

## 2. Architecture (golden path)

```
                              ┌───────────── server (no inbound ports) ─────────────────┐
 User ──HTTPS──► Cloudflare   │                                                          │
                 DDoS · WAF · │  cloudflared (per app) ──► dokploy-traefik:80 ──► app    │
                 Bot Fight ·  │◄═ outbound tunnel :7844 ═╝        (Host routing)    │    │
                 Access*      │                                                     ▼    │
                              │                                    postgres (Dokploy-managed)
                              │                                         │ backups        │
 Admin/CI ══ Tailscale ═══════╪══► tailscale0: SSH · Dokploy panel :3000                 │
                              └─────────────────────────────────────────┼────────────────┘
                                                                         ▼
                                                              R2 (S3-compatible) bucket
  * Access applies to `access:` gated hostnames; public apps rely on in-app auth (Clerk).
```

There are four layers, each with one tool:

| Layer | Scope | Tool | Runs from |
|---|---|---|---|
| **Host** | OS, users, SSH, mesh join, firewall, Docker, Dokploy | Ansible | The engine image, in CI |
| **Edge** | Tunnels, DNS, Access, WAF, bot settings, Tailscale ACL and auth keys, R2 buckets | OpenTofu (`stacks/edge`) | The engine image, in CI |
| **Platform** | Dokploy projects, compose services, domains, databases, backups, tunnel connectors | OpenTofu (`stacks/platform`) | The engine image, in CI |
| **Release** | Building and deploying a version of an app | The app repo's own CI (GitHub Actions → Tailscale → Dokploy API) | GitHub |

**Layering rule:** OpenTofu never SSHes into a server, and Ansible never calls a SaaS API except to fetch secrets.

---

## 3. Repositories

| Repo | Contains | Visibility |
|---|---|---|
| `sealedstack` | Generic Ansible roles, OpenTofu modules and stacks, the `justfile`, the toolbox image and docs. Also `config/` during the MVP. | Private. Going public is a late step that only the owner can confirm (D-053). |
| `sealedstack-config` (v0.5) | The user's `config/` directory | Private |
| App repos (e.g. `example-app`, `shop`) | Code, `docker-compose.yml`, `sealedstack.yaml` manifest, release CI | Unchanged |

### Layout

```
sealedstack/
  justfile                      # contributor aliases for the CLI (§10)
  toolbox/Dockerfile            # tofu, ansible, bws, just, sops, aws, check-jsonschema
  schemas/                      # JSON Schemas for every config file (validated before any run)
  ansible/
    inventory.py                # builds inventory from config/servers.yaml (MagicDNS names)
    playbooks/{bootstrap,site}.yml
    roles/{base,ssh,mesh_tailscale,firewall,docker,platform_dokploy}/
  tofu/
    modules/
      ingress_cloudflare/       # tunnel per app, DNS, WAF, bot settings
      gate_cloudflare_access/   # Access policy + apps + catch-all
      mesh_tailscale/           # ACL, tags, auth keys
      backup_r2/                # buckets + scoped tokens
      platform_dokploy/         # project, compose, domain, db, backups, connector
      secrets_bitwarden/        # read/write secrets by name
    stacks/
      state/                    # creates the state bucket (local state, run once)
      edge/                     # composes ingress + gate + mesh + backup
      platform/                 # composes platform_dokploy for every app × server
  config/                       # ← the ONLY user-specific directory
    stack.yaml
    servers.yaml
    apps.yaml
  docs/
```

---

## 4. Slots and contracts

A **slot** is a replaceable dependency. Each slot has a fixed contract of inputs and outputs, and implementations live in `ansible/roles/<slot>_<impl>` and/or `tofu/modules/<slot>_<impl>`. `config/stack.yaml` picks one implementation per slot.

Abstractions stay minimal. A slot's contract is generalised only when its **second** implementation is written (D-012).

| Slot | Contract: inputs → outputs | MVP implementation | Later implementations |
|---|---|---|---|
| `compute` | server name, bootstrap IP → SSH-reachable Ubuntu 24.04 / Debian 12 host; provider firewall rules (applied, or printed for manual entry) | `any-ssh` (Netcup: firewall rules printed) | `hetzner` (creates server and firewall), `hostinger` |
| `mesh` | server name → private hostname (`<name>`), interface name, CI identity | `tailscale` | `netbird`, `headscale` |
| `ingress` | app, hostnames[], servers[] → per-server connector token; DNS records | `cloudflare` (one tunnel per app) | `pangolin`, `frp` |
| `gate` | hostname, policy name → login enforced at the edge; app-ID claim the connector can check | `cloudflare_access` | `pomerium`, `authentik` |
| `secrets` | name → value (read); name, value → stored (write) | `bitwarden` | `sops`, `gcp`, `infisical`, `aws_ssm`, `azure_kv`, `openbao` |
| `backup` | app, db → S3 endpoint, bucket, prefix, credentials | `r2` | any S3-compatible store (`s3`, `b2`, `wasabi`) |
| `platform` | app manifest, server → running compose, domain routing, DB with scheduled backups | `dokploy` | — |
| `registry` | app image → a registry the platform can pull from; push/pull credentials | `ghcr` (D-033) | dockerhub, quay, generic oci |
| `user_auth` (optional) | app → env keys injected, required DNS records, webhook paths exempted from `gate` | `clerk` | `better_auth`, `zitadel` |

---

## 5. Configuration model

All config files are YAML, validated against `schemas/` before any command runs. **No IP addresses and no secrets** appear in config (D-008).

### 5.1 `config/stack.yaml` — slot selection and global settings

```yaml
domain: example.com
owner_email: you@example.com

slots:
  compute: any-ssh
  mesh: tailscale
  ingress: cloudflare
  gate: cloudflare_access
  secrets: bitwarden
  backup: r2
  platform: dokploy

cloudflare:
  account_id: <from dashboard>          # not a secret
  zone: example.com
  team_name: example                # Access login: example.cloudflareaccess.com

gate:
  policies:
    owner-only: { emails: [you@example.com], session: 24h }
  catch_all: true                       # Access app on *.<domain>, deny-by-default

edge_security:
  bot_fight_mode: true
  block_ai_bots: true
  blocked_paths: ["/.env", "/.git", "xmlrpc.php", "/wp-", "*.php"]

state:
  backend: r2
  bucket: sealedstack-state
```

### 5.2 `config/servers.yaml` — where things can run

```yaml
servers:
  server-1:
    provider: netcup            # netcup | hostinger | hetzner | any-ssh
    role: control               # control = runs the Dokploy panel (exactly one); worker = remote server
    os: ubuntu-24.04
```

The server key is also its Tailscale hostname. The public IP is a bootstrap secret (`SERVER_IP_<NAME>`, D-049) and is never written to config.

### 5.3 `config/apps.yaml` — placement and exposure

```yaml
apps:
  hello:
    source: { image: traefik/whoami:v1.10.3 }   # no repo needed: the smoke-test app
    servers: [server-1]
    hostnames: [hello.example.com]
    access: owner-only

  example-app:
    source: { repo: you/example-app, ref: main }
    servers: [server-1]
    hostnames: [test.example.com]
    access: owner-only

  shop:
    source: { repo: you/shop, ref: main }
    servers: [server-1]
    hostnames: [example.com, www.example.com]
    redirects: { www.example.com: example.com }
    access: public                    # no Access gate; user auth is in-app
    user_auth: clerk
```

`access` is either a policy name from `stack.yaml`, or `public`. A `public` app is still covered by WAF and bot protection.

### 5.4 `sealedstack.yaml` — the app manifest, living in the app repo

This file holds the app's portable intent: no servers, no domains, no secret values.

```yaml
name: shop
compose: docker-compose.yml
web: { service: web, port: 3000, health: /api/health }
needs:
  postgres:
    version: "16"
    database: shop
    env: DATABASE_URL                       # platform injects the connection string here
    backup: { every: 6h, keep: 28 }
    restore_on_create: latest
secrets: [CLERK_SECRET_KEY, CLERK_PUBLISHABLE_KEY, CLERK_WEBHOOK_SECRET]
user_auth:
  webhook_paths: [/api/webhooks/clerk]      # exempted from the gate; the app verifies signatures
```

**Rules for app compose files** (checked by `sealedstack check`):

- No `ports:` on any service. Traffic arrives through Traefik on `dokploy-network`.
- No database service inside the compose file when that database is declared under `needs:`.

### 5.5 Merge rule

`sealedstack debug render` (run as part of `check`, `plan` and `apply`) fetches each app's manifest at `source.ref` through the forge adapter, merges it with the app's `apps.yaml` entry, and writes `.build/apps/<app>.json`. The OpenTofu stacks read only `.build/`, so a plan is reproducible from a rendered snapshot.

---

## 6. Secrets model

| Kind | Store | Example |
|---|---|---|
| Human break-glass (provider root password, `admin` console password) | Bitwarden **Password Manager** (vault entries) | `server-1 root` |
| Machine secrets | Bitwarden **Secrets Manager** | `infra.CLOUDFLARE_API_TOKEN` |
| Personal SSH key | **Bitwarden SSH agent** (desktop app). The private key is never on disk. | `admin@sealedstack` ed25519 |

**Secrets Manager layout.** This fits the Free plan of 3 projects and 3 machine accounts:

| Project | Holds |
|---|---|
| `infra` | Cloudflare API token, Tailscale API credentials, state encryption passphrase, Dokploy API key, Dokploy→worker SSH key |
| `prod` | `<app>.<NAME>` for production apps, e.g. `shop.CLERK_SECRET_KEY` |
| `staging` | The same naming, for staging (v0.2+) |

| Machine account | Access |
|---|---|
| `ci` (the infra repo's `BWS_ACCESS_TOKEN`) | **read-only** access to `infra` and `prod` |
| `app-ci` (per app repo, optional) | read access to `prod` only |
| (spare) | — |

**Flow:**

- sealedstack only **reads** Secrets Manager (D-038). Everything it generates (tunnel tokens, database passwords) lives in the encrypted OpenTofu state, never back in the secret source.
- The platform stack **reads** `<app>.*` and sets the values as Dokploy environment variables.
- Nothing is ever written to git. `.build/` is gitignored.

The only secret the operator handles directly is `BWS_ACCESS_TOKEN`. It is exported in the shell and passed into the toolbox container.

---

## 7. Host baseline (Ansible)

### 7.1 `bootstrap.yml`

Runs once, as `root@<public IP>`, using a password or a provider-installed SSH key:

1. Create the `admin` user with no password, give it passwordless sudo through a validated `/etc/sudoers.d/` drop-in (D-055), and install the public key.
2. `sshd`: `PermitRootLogin no`, `PasswordAuthentication no`, `KbdInteractiveAuthentication no`.
3. Install Tailscale and join with a **single-use, pre-authorised, `tag:server` auth key** (minted by the edge stack; expires after 1 hour), using `--hostname=<name>`.
4. Wait until `<name>` resolves over MagicDNS, then hand off to `site.yml`.

### 7.2 `site.yml`

Runs as `admin@<name>` over Tailscale. It is idempotent, so re-running it is always safe.

| Role | Does |
|---|---|
| `base` | Updates packages, installs `unattended-upgrades` (security updates only, auto-reboot at 04:00), sets timezone and NTP, adds swap if RAM is under 4 GB |
| `ssh` | Re-asserts the sshd hardening |
| `mesh_tailscale` | Ensures `tailscaled` is running and `--ssh=false` (OpenSSH over Tailscale, D-016) |
| `docker` | Installs Docker CE from Docker's apt repo and sets log rotation (`max-size=10m`, `max-file=3`) |
| `firewall` | UFW: deny incoming, allow outgoing, `allow in on tailscale0`, `allow 41641/udp` on the WAN interface; adds a `DOCKER-USER` block (v4 and v6) to `after*.rules` (§7.3) |
| `platform_dokploy` | `control` servers: installs a pinned Dokploy version and binds the panel to the Tailscale interface only. `worker` servers: installs Docker only (Dokploy provisions workers itself, §9.2) |

### 7.3 `DOCKER-USER` rules

These close Docker's well-known bypass of UFW (D-005):

```
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -i tailscale0 -j RETURN
-A DOCKER-USER -i <wan_if> -j DROP
-A DOCKER-USER -j RETURN
```

`<wan_if>` is detected from the default route.

### 7.4 Provider firewall (`compute` slot)

| Provider | MVP behaviour |
|---|---|
| `any-ssh` / `netcup` | `sealedstack server add` prints the exact rule table and waits for the operator to confirm it is applied |
| `hetzner` (v0.4) | `hcloud_firewall` resource via OpenTofu |
| `hostinger` (v0.4) | API if one is usable; otherwise printed, as for Netcup |

The rule table, the same on every provider:

| Direction | Proto | Src port | Dst port | Action | Note |
|---|---|---|---|---|---|
| in | UDP | 53 | * | ACCEPT | DNS replies (needed on stateless firewalls) |
| in | UDP | 123 | * | ACCEPT | NTP replies |
| in | UDP | 7844 | * | ACCEPT | cloudflared QUIC replies |
| in | UDP | * | 41641 | ACCEPT | Tailscale direct path |
| in | ICMP/ICMPv6 | — | — | ACCEPT | PMTU and IPv6 neighbour discovery |
| in | TCP | * | * | DROP | |
| in | UDP | * | * | DROP | |
| out | TCP | * | 25, 465, 587 | DROP | Provider default; outbound email goes through an HTTPS API |

**Public port 22 handling.** It is open only during `bootstrap.yml`. On providers where the firewall is applied by hand, `server add` tells the operator to apply the rule table first and temporarily allow TCP 22, then pauses after bootstrap until they remove that rule.

---

## 8. Edge (OpenTofu `stacks/edge`)

| Resource | Detail |
|---|---|
| Tailscale ACL | **OpenTofu owns the whole policy file** (D-014): `tag:server` and `tag:ci` owned by `autogroup:admin`; admin → `tag:server:*`; `tag:ci` → `tag:server:3000`; `tag:server` → `tag:server:5432` (app replicas reaching their database primary) |
| Tailscale auth key | One per `server-up`: single-use, pre-authorised, `tag:server`, 1 hour expiry |
| Tunnel | **One per app** (D-013), named `ss-<app>`. Ingress for every hostname → `http://dokploy-traefik:80`, with `originRequest.access` set to `{required: true, team_name, aud_tag}` when the app is gated. Its token is written to `infra.tunnel.<app>`. |
| DNS | A proxied CNAME `<host>` → `<tunnel-id>.cfargotunnel.com` for each hostname. Apex hostnames rely on Cloudflare CNAME flattening. |
| Access | Reusable policies from `stack.yaml`; one self-hosted app per gated hostname; a `catch-all` app on `*.<domain>`; a Bypass policy for `user_auth.webhook_paths` |
| WAF | One custom rule, `block-scanner-paths`, built from `edge_security.blocked_paths` |
| Bots | Bot Fight Mode and AI-bot blocking per `edge_security` |
| Redirects | A single redirect rule for each `redirects:` entry (`www` → apex, 301) |
| R2 | `sealedstack-backups` bucket, plus one scoped token for each app's prefix |

---

## 9. Platform (OpenTofu `stacks/platform`)

### 9.1 Implementation choice

Use the community provider `vanillauys/dokploy`, pinned to an exact version. **v0.1 gate:** if that provider cannot manage compose services, domains and environment variables reliably for the hello app, `platform_dokploy` falls back to a small module that calls the Dokploy REST API through `terraform_data` plus `curl`, the same calls `example-app/scripts/deploy.sh` already makes. The module's contract does not change either way (D-011).

### 9.2 Per app × server

| Resource | Detail |
|---|---|
| Project | `<app>`, one per app |
| Compose service | Git source or image source; **auto-deploy off** (the app's CI owns releases, D-015); environment = `secrets:` + `needs` connection strings |
| Domain | Every hostname on `web.service:web.port`, with HTTPS off (TLS ends at the edge) |
| Connector | Compose service `<app>-connector` running `cloudflare/cloudflared:<pinned>` with `TUNNEL_TOKEN` and `TUNNEL_TRANSPORT_PROTOCOL=auto`, on `dokploy-network`, read-only, all capabilities dropped |
| Database | Dokploy-managed Postgres on the **first** server listed (the primary), with a scheduled backup to the `backup` slot destination at prefix `<app>/<db>/` |
| Outputs | The compose ID, published to the app repo as the GitHub Actions secret `DOKPLOY_APP_ID` (v0.2) |

**Worker servers** are registered as Dokploy *remote servers* over their Tailscale hostname, using the Dokploy→worker SSH key from secrets. Dokploy installs Traefik on the worker.

---

## 10. Commands

Every command is `sealedstack <noun> <verb>`. It is the same vocabulary as a workflow input, a `/sealedstack` pull request comment and, optionally, a terminal in a Codespace (D-042). `check` runs before every other command. Locally, only the read-only commands work; writes need CI or the guarded `--break-glass` (D-045). The full reference, with approvals, is [user-experience.md §12.1](user-experience.md#121-commands-ci-input-pr-comment-and-local-all-the-same).

| Command | Does |
|---|---|
| `sealedstack setup` | Writes `config/` and opens the Setup PR with the living checklist |
| `sealedstack check` | Preflight: schemas, secrets, token permissions, version pins; read-only |
| `sealedstack plan` | Renders the app manifests, then plans edge and platform; read-only |
| `sealedstack apply` | Renders, then applies edge and platform from the merged `main` |
| `sealedstack up` | Converges everything: state bucket, edge, server bootstrap and baseline, platform, verify, Status issue; re-running resumes |
| `sealedstack server add <name>` (`add-server`) | Mints a mesh auth key, runs `bootstrap.yml`, then `site.yml`; prints the provider firewall table |
| `sealedstack server sync [name]` | Re-runs `site.yml` (drift correction, upgrades) |
| `sealedstack app add` (`add-app`) | Opens the infra PR and the app-repo onboarding PR |
| `sealedstack db restore <app> [backup]` (`restore`) | Restores the latest (or a named) backup into the app's primary database |
| `sealedstack db drill <app>` (`drill`) | Restores the latest backup into a throwaway database and checks it |
| `sealedstack status` | Health, versions, backups and drift; updates the Status issue |
| `sealedstack debug render\|publish\|providers` | Internals for troubleshooting |

---

## 11. Data: backup and restore

- **Backup:** Dokploy's scheduled database backup goes to an S3-compatible destination created from the `backup` slot. Stored objects are `<app>/<db>/<timestamp>.sql.gz`. Retention is `keep: N`.
- **Dokploy self-backup:** the panel's own database is backed up daily to `_dokploy/`.
- **Restore on create:** when the platform stack creates a database for an app with `restore_on_create: latest`, it starts a one-time restore of the newest object before the app's compose deploys.
- **Restore drill (v0.3):** a weekly scheduled job restores the latest dump into a throwaway database, runs `SELECT 1` plus a row count on a table the manifest names, then drops it. A failure triggers a Dokploy notification.
- **Recovery inputs:** git + Bitwarden + the R2 bucket. Nothing else is required.

---

## 12. Scaling model

- `servers: [a, b]` gives one compose deployment and one tunnel connector **on each** server. Cloudflare sends each request to the nearest healthy connector, which provides failover across servers.
- The database stays on `servers[0]`. Replicas connect to it over Tailscale (`<primary>:5432`, allowed by the ACL and by `DOCKER-USER` on `tailscale0`).
- Apps must be stateless apart from the database: sessions are Clerk tokens, and uploads go to object storage.
- **Not provided:** database HA, even load balancing (that requires the paid Cloudflare Load Balancer). See roadmap.

---

## 13. Security model

| Threat | Control |
|---|---|
| Internet scans of the origin IP | Provider firewall drops at the hypervisor; UFW denies by default; `DOCKER-USER` drops new connections on the WAN interface |
| Bots and scanners via the hostname | WAF path rule, Bot Fight Mode, then Access (gated apps) |
| A gated hostname exposed by mistake | The catch-all Access app on `*.<domain>`, and the connector validates the Access token (`originRequest.access.required`) |
| Unauthenticated app requests (public apps) | In-app auth (Clerk); Turnstile on sign-up (v0.2+) |
| Stolen laptop | SSH key held in the Bitwarden agent (locked vault); `BWS_ACCESS_TOKEN` revocable; Tailscale device revocable |
| CI compromise | `tag:ci` can reach only port 3000 (the Dokploy API); its machine account reads `prod` only |
| Leaked secrets in git | No secret values in config; `.build/` gitignored; gitleaks pre-commit hook |
| Supply chain | All images and tools pinned to exact versions; Renovate PRs (roadmap) |
| Losing the state file | Encrypted with OpenTofu state encryption (passphrase in Bitwarden) and stored in R2 |

---

## 14. Prerequisites

Users install nothing locally: a browser, a Git hosting account and the credentials below are enough (D-034, D-044). The Setup checklist verifies every item. The step-by-step version is [prerequisites.md](prerequisites.md).

| Requirement | Mandatory | Notes |
|---|---|---|
| A domain on Cloudflare nameservers | ✅ | Tunnel hostnames and Access require a Cloudflare zone |
| Cloudflare account (Free plan) with Zero Trust enabled | ✅ | Zero Trust and R2 may ask for a payment method even on free plans |
| Cloudflare API token (`CLOUDFLARE_API_TOKEN`) | ✅ | Account: Cloudflare Tunnel Edit, Access Apps and Policies Edit, Workers R2 Storage Edit. Zone: DNS Edit, Zone WAF Edit, Bot Management Edit, Single Redirect Edit. The R2 S3 keys are derived from it (D-044, D-054): key id = the token's id, secret = SHA-256 of the token value; `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` are an optional override |
| Tailscale account with MagicDNS on, and an OAuth client | ✅ | Scopes `policy_file`, `auth_keys`, `devices:core`. Server auth keys are minted from it; no long-lived API key |
| `STATE_PASSPHRASE` | ✅ | At least 16 characters; encrypts the OpenTofu state (key name configurable in `secrets.keys`, default `STATE_PASSPHRASE`; the older key `state_passphrase` is read as a deprecated fallback) |
| Bitwarden account plus a free organisation with Secrets Manager | ✅ | One **read-only** machine-account token, held in CI as `BWS_ACCESS_TOKEN` |
| Server credentials, per existing server | ✅ for Netcup / any-ssh | `SERVER_IP_<NAME>` and `ROOT_PASSWORD_<NAME>`; the key names are declared in `servers.yaml` under `bootstrap` (D-049, D-050). API providers need only their token |
| `admin.ssh_keys` in `stack.yaml` | ✅ | Operator SSH public keys, installed at bootstrap (not secrets, D-040) |
| Bot identity (`sealedstack-bot`) | ✅ | A user-owned GitHub App created from the Setup checklist's manifest link (D-039) |
| A GitHub account and a private infra repository | ✅ | Hosts the two bootstrap files and runs the engine in GitHub Actions |
| A server: Ubuntu 24.04 or Debian 12, ≥ 2 vCPU / 4 GB / 40 GB (control) | ✅ | Workers: ≥ 1 vCPU / 2 GB |
| Clerk account | Only for `user_auth: clerk` apps | |

---

## 15. Delivery plan

### v0.1: Empty box → hello app (the MVP)

**Scope:**

- Repo skeleton, toolbox image, `schemas/`, `sealedstack check`.
- `state` stack on R2 with state encryption.
- Ansible: `bootstrap.yml` and `site.yml` with every role in §7.
- Edge: Tailscale ACL and auth key; tunnel, DNS, Access (policy + app + catch-all), WAF rule and bot settings for the `hello` app.
- Platform: Dokploy project, image-source compose, domain and connector for `hello` (including the provider-vs-REST-fallback gate in §9.1).
- Docs: the prerequisites checklist and the Netcup firewall procedure.

**Acceptance criteria**, on a freshly wiped Netcup server:

1. `sealedstack up` (Run workflow, then one approval) finishes without errors in ≤ 15 minutes from an empty server (not counting the manual firewall step).
2. From an external network, TCP ports `22, 80, 443, 3000, 8080` on the public IP all time out.
3. `https://hello.example.com` returns 302 to the Access login; after logging in as the owner, whoami output shows.
4. `https://hello.example.com/.env` returns 403.
5. `ssh server-1` works over Tailscale, and `http://server-1:3000` shows the Dokploy panel.
6. Re-running `sealedstack server sync` and `sealedstack apply` produces **no changes**.
7. `git grep` finds no secret values; `config/` contains no IP addresses.

### v0.2: Real apps

example-app and shop onboarded; `sealedstack.yaml` manifests; manifest rendering; Clerk plumbing (secret injection, DNS-only Clerk records, webhook bypass); the `www` → apex redirect; `DOKPLOY_APP_ID` published to the app repos; removing `ports:` and the in-compose Postgres from example-app.

### v0.3: Durable data

R2 backup destination, Dokploy-managed Postgres, `restore_on_create`, `sealedstack db restore`, weekly restore drill, Dokploy self-backup, and a documented full rebuild drill.

### v0.4: Multi-server

`compute: hetzner` (server creation and firewall), `compute: hostinger`, worker registration, and `servers: [a, b]` replicas.

### v0.5: Developer experience and open source (beta)

`sealedstack setup`, `sealedstack check`, the provider release gate; the `secrets: sops`
implementation; the `config/` split into its own repo (`SEALEDSTACK_CONFIG`);
public docs; CI for this repo (ansible-lint, `tofu validate`, gitleaks); publish
the toolbox image to GHCR. Kept in beta until v1.0.

### M0–M5

The infra-repo/release track inserts milestones **M0** (green and safe), **M1**
(tool image + infra repo), **M2** (build modes + registry + ownership), **M3**
(example-app), **M4** (live v0.1 on the wiped Netcup server) and **M5** (shop).
v1.0 below is the release gate over all of them.

### v1.0: Validated release

The v0.1 acceptance criteria pass on a real server **and at least half of the
registered providers are `supported`** (live-tested). Recorded in
verification log (private); the README status flips to stable.

---

## 16. Risks and open questions

| # | Item | Mitigation |
|---|---|---|
| R1 | The `vanillauys/dokploy` provider is community-maintained (one maintainer) | Exact pin; REST fallback behind the same module contract (§9.1) |
| R2 | Bitwarden's Free plan limits (3 projects / 3 machine accounts) could change | `secrets` slot; `sops` is available (v0.5) |
| R3 | Clerk is US-hosted only | Accepted for now (D-003); revisit if EU residency is ever needed |
| R4 | Cloudflare Free plan limits on WAF rules, redirect rules and Access users | The MVP uses 1 WAF rule, 1 redirect rule and 1 Access user |
| R5 | The Netcup firewall cannot be automated in the MVP | A printed rule table plus a confirmation step |
| R6 | Dokploy's Traefik container and network names (`dokploy-traefik`, `dokploy-network`) are assumed | Checked by an Ansible assert after the Dokploy install |
| R7 | Cloudflare only supports cloudflared releases from the last 12 months | Pinned tag; quarterly bump (Renovate in the roadmap) |
| R8 | GHCR / GitHub Actions pricing can change | The `registry` slot lets you move registries; image retention keeps usage small |
| R9 | Dokploy API keys may not be project-scoped | Each app CI may hold the shared key; mitigate with a `production` environment + required reviewers |
| R10 | HuJSON parse/merge fidelity | A pinned JSON5 parser; `plan` shows a readable policy diff before apply |
| Q1 | Does Bitwarden's official OpenTofu/Terraform provider cover writing secrets, or do we shell out to `bws`? | Check during v0.1 |
| Q2 | Can Dokploy's API start a restore from a named backup object? This is needed for `restore_on_create`. | Check during v0.3; fallback is a one-time `pg_restore` job container |
