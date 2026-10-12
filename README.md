# sealedstack

**A sealed origin for your apps.** sealedstack turns an empty Linux box you can
SSH into into a hardened server with **zero inbound ports**: administration over
a private mesh, public traffic only through an outbound tunnel with a gate in
front, and the whole stack rebuildable from git, a secrets manager and object
storage.

> **Pre-release (0.5).** The engine, the Terraform/Ansible code and the CI
> workflows validate offline (unit tests, `tofu validate`, `ansible-lint`) and
> against mocked APIs. **Nothing has run against live accounts yet, so no provider
> is marked `supported`.** Providers become `supported` only after the first live
> validation run on a real server, and **v1.0** is the release where that run
> passes and at least half of the registered providers are `supported`. The
> repository and its images are private until then.

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

Four layers, one tool each: **Host** (Ansible), **Edge** (OpenTofu),
**Platform** (OpenTofu), **Release** (the app repo's CI). You install none of
them: the engine is a pinned container image that runs in **your own CI**, driven
by your own infra repo. Every change is a reviewed pull request.

Every external dependency is a pluggable **slot** — one provider per slot,
catalogued in `registry/` and dispatched by config (spec §4). See
[docs/architecture.md](docs/architecture.md) for the detailed design.

## Quickstart

**Zero installs.** You need a browser, a GitHub account and the accounts listed in
[prerequisites](docs/prerequisites.md) (a domain on Cloudflare, Tailscale,
Bitwarden Secrets Manager and a server you can SSH into). Everything runs in
GitHub Actions. The full walkthrough, with every screen, is in
[getting started](docs/getting-started.md) and the
[user experience](docs/user-experience.md).

1. **Create an empty private repository** (any name, for example `my-infra`).
   In its settings, enable *Allow GitHub Actions to create and approve pull
   requests*.
2. **Add two files** on `main`: `sealedstack.version` (one line, the version you
   are installing) and `.github/workflows/sealedstack.yml` (copied from
   [getting started](docs/getting-started.md#2-add-two-files)).
3. **Actions, sealedstack, Run workflow, command `setup`**, with
   `--domain example.com --email you@example.com --admin admin --server server-1 --provider netcup --secrets bitwarden`.
   Setup opens **PR #1** with `config/` and a living checklist.
4. **Work through the checklist.** Each item links to the exact page and
   permission list. Put the values in Bitwarden (only `BWS_ACCESS_TOKEN` goes into
   GitHub) and comment `/sealedstack check` whenever you like. The checklist
   updates in place.
5. **Merge PR #1** when every item is green. Nothing outside the repository has
   changed yet.
6. **Run workflow, command `up`**, and approve it in the `production`
   environment. It creates the state bucket, edge, server baseline and a first
   `hello` app, then verifies that no port is open.

`https://hello.example.com` then answers with a redirect to the Access login.
After that, every change is a pull request: edit `config/`, read the plan comment,
merge, approve.

**Optional: Codespaces.** Setup also generates `.devcontainer/devcontainer.json`
pinned to the engine image, so a Codespace gives you a terminal with
`sealedstack` ready, with nothing installed on your machine. Locally it is
read-only (`check`, `plan`, `status`); writes happen in CI.

## Providers

Every dependency is a **slot** with a registry of implementations (`registry/`).
You swap providers with one line in `config/stack.yaml`, or per server in
`config/servers.yaml`. List them with `sealedstack debug providers [slot]`.

### Golden path (the supported scope for v1)

| Slot | Golden path | Status |
|---|---|---|
| compute | any-ssh / netcup (existing server), hetzner (created for you) | experimental |
| mesh | tailscale | experimental |
| ingress | cloudflare (Tunnel) | experimental |
| gate | cloudflare_access | experimental |
| secrets | bitwarden (read-only) | experimental |
| backup / state | r2 | experimental |
| platform | dokploy | experimental |
| registry | ghcr | experimental |
| user_auth | clerk | experimental |
| forge / ci | github | experimental |

**Status:** every entry above is **experimental** (the code exists and is tested
offline, but has never run against live accounts). **Nothing is `supported` until
the first live validation run.**

### Experimental: implemented, never run live

Implemented behind the same contracts and tested offline only. Expect rough edges.

| Slot | Other implementations |
|---|---|
| compute | hostinger, digitalocean, vultr, linode, scaleway, aws, gcp, azure, oracle, upcloud, ovh, contabo, civo |
| mesh | headscale, netbird, zerotier, nebula, netmaker, innernet, firezone, wireguard, tailscale-ssh |
| ingress | ngrok, caddy, traefik, nginx, frp, pangolin, tailscale-funnel, zrok, bore |
| gate | authentik, authelia, oauth2-proxy, pomerium, keycloak, zitadel, logto, okta, auth0 |
| secrets | sops, aws_ssm, gcp, azure_kv, openbao, vault, infisical, onepassword, doppler, pass |
| backup | 14 S3-compatible stores, gcs, azure_blob, hetzner_storage_box |
| platform | coolify, caprover, portainer (planned: kamal, kubernetes, nomad, fly, render, railway) |
| user_auth | oidc, better_auth, zitadel, logto, authentik, keycloak, supabase, firebase, workos, auth0 |
| ci | gitlab-ci, gitea-actions, drone, jenkins, buildkite, circleci, bamboo, woodpecker, aws-codebuild |
| registry | (planned: dockerhub, quay, oci) |

Adding a provider is a registry entry plus a module, role or adapter. See
[docs/providers.md](docs/providers.md) and [AGENTS.md](AGENTS.md).

## Commands

One vocabulary, everywhere: as a workflow input, as a `/sealedstack <command>`
pull request comment (writers only) and, optionally, in a Codespace terminal as
`sealedstack <command>`. Full reference:
[user experience, section 12](docs/user-experience.md#12-reference).

| Command | Where it runs | Approval | Does |
|---|---|---|---|
| `setup` | Run workflow | none | Opens the Setup PR with `config/` and the living checklist |
| `check` | `/sealedstack check`, automatically on PRs | none | Preflight: verifies config, secrets, permissions and versions before anything changes |
| `plan` | Automatically on PRs | none | Shows what would be created, changed or destroyed |
| `apply` | Automatically after merge to `main` | yes | Applies the merged config |
| `up` | Run workflow | yes | Converges everything from an empty server to a running app; re-running resumes |
| `add-server` (`server add`) | Run workflow | yes | Bootstraps an existing machine and opens a PR recording it |
| `add-app` (`app add`) | Run workflow | none | Opens the infra PR and the app-repo onboarding PR |
| `status` | Hourly and after every apply | none | Updates the pinned Status issue |
| `restore` (`db restore`) | Run workflow | yes + typed name | Restores a database backup |
| `drill` (`db drill`) | Weekly and Run workflow | none | Restores a backup into a throwaway database and checks it |

Write commands (`apply`, `up`, `add-server`, `restore`, `server sync`) run only
from a merged pull request on `main` and only after an approver in
`stack.yaml: approvers` has agreed. sealedstack enforces this itself, so it also
holds on private GitHub Free repositories.

## Documentation

Start with [docs/README.md](docs/README.md):

- **Get started:** [getting-started](docs/getting-started.md), [prerequisites](docs/prerequisites.md), [config repository](docs/config-repo.md)
- **Known limitations:** [limitations and caveats](docs/limitations.md) — what does not work yet, and the requirements behind it
- **Configure & operate:** [providers](docs/providers.md), [CI/CD](docs/ci-cd.md), [netcup firewall](docs/netcup-firewall.md)
- **Architecture:** [architecture](docs/architecture.md)
- **Design:** [spec](docs/spec.md), [decisions](docs/decisions.md), [user experience](docs/user-experience.md)
- **Distribution:** [infra repo + tool image](docs/infra-repo.md)
- **Contributing:** [CONTRIBUTING.md](CONTRIBUTING.md), [development](docs/development.md), [AGENTS.md](AGENTS.md)

## Licence

[Apache-2.0](LICENSE).
