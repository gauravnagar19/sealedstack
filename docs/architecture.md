# Architecture

This is the detailed design. [spec.md](spec.md) is the original design document;
this page is the implementation-level view of how the pieces fit together.

## Goals

An empty Linux box you can SSH into becomes a **sealed origin**: no inbound
ports, admin access over a private mesh, public traffic through an outbound
tunnel with a gate in front, and everything rebuildable from git + a secrets
manager + object storage.

Design invariants:

- No inbound port is open to the internet (spec D-001).
- OpenTofu never SSHes into a host; Ansible never calls a SaaS API except to
  fetch secrets.
- The generic code contains no user-specific values; those live in `config/`
  (spec D-008).
- Any external dependency is a **slot** with a written contract, so providers
  are swappable (spec D-012).

## Layers

```
                    ┌──────────────────── server (no inbound ports) ────────────────────┐
   Users ──HTTPS──► Cloudflare / ngrok ──┐
   DDoS·WAF·Bots·Access                  │   connector ─► dokploy-traefik:80 ─► app
                                         └─► (outbound tunnel)          │
                                                                       ▼
                                                          postgres (Dokploy-managed)
                                                              │ scheduled backups
   Admins / CI ══ Tailscale ══► tailscale0: SSH · Dokploy :3000        ▼
                    (private mesh)                            R2 / S3 (object storage)
```

| Layer | Scope | Tool | Runs from |
|---|---|---|---|
| **Host** | OS, users, SSH, mesh join, firewall, Docker, Dokploy, self-hosted gate/ingress | Ansible (`ansible/`) | the engine image, in CI |
| **Edge** | tunnels, DNS, Access/gate, WAF, bot rules, Tailscale ACL, backup destination | OpenTofu (`tofu/stacks/edge`) | the engine image, in CI |
| **Platform** | Dokploy projects, compose services, domains, databases, backups, tunnel connectors, workers | OpenTofu (`tofu/stacks/platform`) | the engine image, in CI |
| **Release** | building and deploying an app version | the app repo's CI (`ci` slot) | the app's CI |

There is also a **compute** stack (`tofu/stacks/compute`) that provisions or
configures servers and their provider firewalls, and a one-shot **state** stack.

## Request and data flow

- **User traffic** hits Cloudflare (WAF, bot rules, Access for gated hostnames)
  and travels down a per-app tunnel to a connector container, which forwards to
  Dokploy's Traefik, which routes by hostname to the app.
- **Admin traffic** (SSH, the Dokploy panel) is reachable only over the mesh
  interface (`tailscale0` by default), never the public IP.
- **CI** joins the mesh as `tag:ci` and calls the Dokploy API on port 3000 to
  deploy a version (D-015).
- **Backups** flow from Dokploy-managed databases to an S3-compatible bucket on
  a schedule.

## Slots and providers

Every external dependency is a slot with a contract (spec §4). The catalog lives
in `registry/<slot>.yaml` and is validated by `schemas/registry.schema.json`.
`sealedstack debug providers` lists it.

```
config/stack.yaml   slots: { compute: hetzner, backup: r2, gate: cloudflare_access, ... }
        │
        ▼
registry/<slot>.yaml  ──►  sealedstack check (selected impl must exist & not be planned;
        │                        required secrets and permissions)
        ▼
stacks dispatch on the slot  ──►  tofu/modules/<slot>_<impl>  |  ansible/roles/<slot>_<impl>  |  scripts/<adapter>
```

Dispatch:

- **compute** — per server (`config/servers.yaml` `provider`); `stacks/compute`
  has one module block per provider. A provider with `creates_server: true` is
  provisioned by `sealedstack up` / `server add`, which then discovers the IP.
- **mesh / ingress / gate / secrets / backup** — the edge stack branches on
  `local.slots.<slot>`. `tofu/stacks/edge/main.tf` is the reference for this.
- **platform** — `stacks/platform` (Dokploy today).
- **user_auth / ci** — config-only; injected into app environments and the
  release pipeline.

Because `validate` fails when a selected implementation is missing or `planned`,
you never get a silent wrong deploy. Adding a provider is a registry entry plus
a module/role/adapter (see [AGENTS.md](../AGENTS.md)).

## State, secrets, backups

- **State** — each stack has an S3 backend pointing at an R2 bucket created by
  the `state` stack, with OpenTofu native encryption using
  `infra.state_passphrase` (D-007). `engine/sealedstack/tofu.py` wires it.
- **Secrets** — Bitwarden by default; the `secrets` slot can be `sops`,
  `aws_ssm`, `gcp`, `azure_kv`, `openbao` or `vault`. Reads go through
  `engine/sealedstack/secrets.py`; the edge stack writes tunnel tokens back.
- **Backups** — a `dokploy_destination` on the selected backup slot; per-DB
  scheduled backups with retention, an optional second destination (3-2-1), and
  `sealedstack db restore` / `db drill`. Dokploy's own DB is backed up to
  `_dokploy/`.

## Reproducible rendering

`sealedstack debug render` (run by `check`, `plan` and `apply`) fetches each app's `sealedstack.yaml` manifest and merges it with
its placement entry, writing `.build/`. The tofu stacks read only `.build/`, so
a plan is reproducible from a rendered snapshot. `.build/env.json` (written by
`sealedstack.buildenv` + `sealedstack.userauth`) carries app secrets and
user-auth config into the platform stack.

## Security model

Layers of defence (spec §13):

| Threat | Control |
|---|---|
| Internet scans of the origin IP | provider firewall + UFW deny + `DOCKER-USER` drop on the WAN interface (D-005) |
| Bots/scanners via the hostname | WAF path rules, Bot Fight Mode, then Access for gated hostnames |
| A gated hostname exposed by mistake | catch-all Access app on `*.<domain>` + connector validates the Access token |
| Unauthenticated public app | in-app auth (Clerk/OIDC) |
| Stolen laptop | SSH key in the Bitwarden agent; `BWS_ACCESS_TOKEN` and the Tailscale device are revocable |
| CI compromise | `tag:ci` reaches only port 3000; its machine account reads `prod` only |
| Leaked secrets in git | no secrets in config; `.build/` gitignored; gitleaks in CI + pre-commit |
| Supply chain | images and tools pinned; Renovate opens PRs |
| Losing state | encrypted state in R2 |

## Repository map

```
justfile            contributor aliases for the CLI (runs in the toolbox)
toolbox/Dockerfile  the pinned image (tofu, ansible, bws, sops, aws, ...)
config/             the only user-specific values (+ *.example.yaml)
registry/           provider catalog, one file per slot
schemas/            JSON Schemas for config + registry
scripts/            glue: render, validate, doctor, compute, edge, platform, ...
ansible/            host roles + playbooks (site.yml, bootstrap.yml)
tofu/modules/       provider implementations (<slot>_<impl>)
tofu/stacks/        state | edge | platform | compute
examples/           app template (manifest + compose + release CI)
docs/               user + design docs (see docs/README.md)
```

## Extending

See [AGENTS.md](../AGENTS.md) for the provider recipes, conventions and the
validation gate. Detailed build planning lives outside this repository.
