# sealedstack

**A sealed origin for your apps.** sealedstack turns an empty Linux box you can
SSH into into a hardened server with **zero inbound ports**: administration over
a private mesh, public traffic only through an outbound tunnel with a gate in
front, and the whole stack rebuildable from git, a secrets manager and object
storage.

> **Version 0.5 (pre-release).** The toolchain validates offline
> (`just validate`, `tofu validate`, `ansible-lint`) and providers are
> code-complete, but **nothing has run against live accounts yet**. The test
> and verification status is tracked in the private development repo.
>
> **v1.0** is the validated release: it is reached when the v0.1 acceptance
> criteria pass on a real server **and at least half of the registered providers
> are `supported`** (live-tested). Check with `just release-status`.

---

## Why

Opening 22/80/443 to the internet means constant scanning and patch pressure.
sealedstack removes the attack surface instead of defending it:

- **Nothing to scan.** The provider firewall, UFW and Docker's `DOCKER-USER`
  chain all drop inbound traffic. A port that is not open cannot be attacked.
- **Admins over a private mesh.** SSH and the Dokploy panel are reachable only
  over Tailscale (or another mesh), never the public internet.
- **Users through an outbound tunnel.** Cloudflare Tunnel (or ngrok) delivers
  traffic to the origin; WAF, bot rules and Cloudflare Access run at the edge,
  before anything touches your server.
- **Rebuildable.** State in encrypted R2, secrets in Bitwarden, configuration in
  git. A host is disposable.

## Architecture

```
                    ┌─────────────────── server (no inbound ports) ───────────────────┐
  User ──HTTPS──►   │                                                                │
  Cloudflare        │   connector ──► dokploy-traefik:80 ──► app                     │
  DDoS·WAF·Bots·    │   (cloudflared / ngrok, outbound)          │                    │
  Access*           │                                            ▼                    │
                    │                              postgres (Dokploy-managed)         │
                    │                                    │ scheduled backups         │
  Admin/CI ═Tailscale═► tailscale0: SSH · Dokploy panel :3000                        │
                    └────────────────────────────────────────┼───────────────────────┘
                                                              ▼
                                                   R2 / S3 (object storage)
  * Access applies to gated hostnames; public apps use in-app auth.
```

Four layers, one tool each — **Host** (Ansible), **Edge** (OpenTofu),
**Platform** (OpenTofu), **Release** (the app repo's CI). Everything runs from a
single `just` entrypoint inside a pinned toolbox image.

Every external dependency is a pluggable **slot** — one provider per slot,
catalogued in `registry/` and dispatched by config (spec §4). See
[docs/architecture.md](docs/architecture.md) for the detailed design.

## Quickstart

**Prerequisites:** a domain on Cloudflare, a Tailscale account, Bitwarden
Secrets Manager, `gh`, Docker and `just` on your laptop, and a server. Full
details in [docs/getting-started.md](docs/getting-started.md).

```sh
brew install just                       # the one host tool

just init                               # write config/ (or copy the *.example.yaml)

export BWS_ACCESS_TOKEN=...             # Bitwarden machine-account token
export GH_TOKEN=...                     # so `just render` reads app manifests
# add your public key(s) to config/stack.yaml under admin.ssh_keys
export ROOT_PASSWORD=...                # provider root password (first boot)

just validate                           # schema + registry + compose lint
just doctor                             # checks secrets/tools for your providers
just state-init                         # create the encrypted state bucket (once)
just server-up web-1                    # create + bootstrap (hetzner/DO/vultr/... )
just apply                              # edge + platform
```

Then `curl -sI https://hello.example.com` returns a 302 to the Access login.

## Providers

Every dependency is a **slot** with a registry of implementations, so you swap
providers with one line in `config/stack.yaml` (or per server in
`config/servers.yaml`).

```sh
just providers            # everything
just providers backup     # one slot
```

Generated from `registry/`; every implemented entry is **experimental** (code
exists, not yet live-tested), so `status` is shown per slot.

| Slot | Implemented (experimental) | Planned |
|---|---|---|
| compute | any-ssh, netcup, hetzner, hostinger, digitalocean, vultr, linode, scaleway, aws, gcp, azure, oracle, upcloud, ovh, contabo, civo | — |
| mesh | tailscale, headscale, netbird, zerotier, nebula, netmaker, innernet, firezone, wireguard, tailscale-ssh | — |
| ingress | cloudflare, ngrok, caddy, traefik, nginx, frp, pangolin, tailscale-funnel, zrok, bore | — |
| gate | cloudflare_access, authentik, authelia, oauth2-proxy, pomerium, keycloak, zitadel, logto, okta, auth0 | — |
| secrets | bitwarden, sops, aws_ssm, gcp, azure_kv, openbao, vault, infisical, onepassword, doppler, pass | — |
| backup | r2 + 14 S3-compatible stores, gcs, azure_blob, hetzner_storage_box | — |
| platform | dokploy, coolify, caprover, portainer | kamal, kubernetes, nomad, fly, render, railway |
| user_auth | clerk, oidc, better_auth, zitadel, logto, authentik, keycloak, supabase, firebase, workos, auth0 | — |
| ci | github-actions, gitlab-ci, gitea-actions, drone, jenkins, buildkite, circleci, bamboo, woodpecker, aws-codebuild | — |
| registry | ghcr | dockerhub, quay, oci |

Adding a provider is a registry entry plus a module, role or adapter — see
[docs/providers.md](docs/providers.md) and [AGENTS.md](AGENTS.md).

## Commands

| Command | Does |
|---|---|
| `just validate` | Schema-check `config/`, check the registry, render manifests, lint compose |
| `just providers [slot]` | List the provider registry |
| `just release-status` | Verified-provider ratio and the v1.0 gate |
| `just doctor` | Check required secrets and tools |
| `just init` | Interactive wizard that writes `config/` |
| `just state-init` | Create the encrypted state bucket (once) |
| `just render` | Fetch app manifests into `.build/` |
| `just server-up <name> [ip]` | Provision/configure a server and join the mesh |
| `just server-sync [name]` | Re-run the host baseline (drift correction) |
| `just plan` / `just apply` | Render, then plan/apply edge, then platform |
| `just app-restore <app> [backup]` | Restore a database backup |
| `just db-drill <app> [table]` | Restore a backup into a throwaway DB and check it |
| `just secrets-write` | Write secrets to a SOPS file (`secrets: sops`) |

## Documentation

Start with [docs/README.md](docs/README.md):

- **Get started:** [getting-started](docs/getting-started.md), [prerequisites](docs/prerequisites.md), [adopting existing resources](docs/adopt.md), [config repository](docs/config-repo.md)
- **Known limitations:** [limitations and caveats](docs/limitations.md) — what does not work yet, and the requirements behind it
- **Configure & operate:** [providers](docs/providers.md), [CI/CD](docs/ci-cd.md), [operations](docs/operations.md), [security](docs/security-hardening.md), [monitoring](docs/monitoring.md)
- **Architecture:** [architecture](docs/architecture.md)
- **Design:** [spec](docs/spec.md), [decisions](docs/decisions.md)
- **Distribution:** [infra repo + tool image](docs/infra-repo.md) · [design spec (infra/release)](docs/spec-infra-repo-and-release.md)
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md), [development](docs/development.md), [AGENTS.md](AGENTS.md)

## Licence

[Apache-2.0](LICENSE).
