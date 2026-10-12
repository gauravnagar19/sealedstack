# The infra repo

sealedstack is not a repo you clone and fill in. It is a **versioned tool image
with a CLI** (D-031). Each user keeps their own **infra repo**: a small, private
repository that holds *configuration* and *pins a sealedstack version*. The
sealedstack source is never copied in, and the only thing the laptop needs is
**Docker**.

This page describes that model. For the design, see
[spec.md](spec.md); for the
commands, see [getting-started.md](getting-started.md).

## Three repositories

```
┌──────────────────────────────┐  pins version   ┌───────────────────────────────┐
│ sealedstack  (the tool)      │◄────────────────│ <name>-infra  (user's config) │
│ public · versioned · image:  │                 │ private · chosen at `init`    │
│ ghcr.io/<owner>/sealedstack  │                 │ e.g. my-infra              │
└──────────────┬───────────────┘                 └──────────────┬────────────────┘
               │ lists apps; publishes deploy settings           │
               ▼                                                ▼
        ┌──────────────────────────────────────────────────────────────┐
        │ app repos: code, compose, sealedstack.yaml, release CI        │
        └──────────────────────────────────────────────────────────────┘
```

| Repo | Owns | Never contains |
|---|---|---|
| **sealedstack** (the tool) | Ansible roles, OpenTofu modules and stacks, scripts, schemas, registry, the toolchain image, docs | Any user's domain, servers, apps, emails or IPs |
| **Infra repo** (`<name>-infra`) | `config/` (stack, servers, apps, Tailscale policy, provider overrides), the pinned version, its own plan CI | Secrets, sealedstack source code |
| **App repos** | Code, `docker-compose.yml`, `sealedstack.yaml`, release CI | Server names, hostnames, access policies, infra repo references |

Links point one way (D-023): the infra repo's `apps.yaml` names app repos; app
repos never name an infra repo. Anything app CI needs is **published by the
infra repo** into the app repo's GitHub variables and secrets.

## Layout

`sealedstack init` creates this (D-028 asks for `<name>`, e.g. `my-infra`):

```
my-infra/
├── sealedstack.version            # e.g. "0.5.0" — the only place the tool version is set
├── bin/sealedstack                # tiny wrapper (generated): docker run the pinned image
├── config/
│   ├── stack.yaml                 # domain, slots, cloudflare, gate, edge_security, mesh, registry, state
│   ├── servers.yaml               # server-1 …
│   ├── apps.yaml                  # placement + exposure per app
│   ├── tailscale-policy.hujson    # YOUR tailnet policy, imported at init (D-026)
│   └── providers/                 # optional per-provider non-secret overrides
├── .github/workflows/sealedstack.yml  # calls the reusable infra.yml (plan on PRs, never apply)
├── .gitignore                     # .sealedstack/, *.tfstate*
└── README.md                      # generated: what this repo is, how to run it
```

## Running the tool

`bin/sealedstack` is the **only** entrypoint. The host needs **Docker only** —
no `just`, no clone of sealedstack. The generated wrapper reads the pinned
version and runs the image against this repo:

```bash
#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
version="$(cat "$here/sealedstack.version")"
exec docker run --rm -i $( [ -t 0 ] && echo -t ) \
  -v "$here:/infra" -w /infra \
  -e SEALEDSTACK_CONFIG=/infra/config -e SEALEDSTACK_WORKDIR=/infra/.sealedstack \
  -e BWS_ACCESS_TOKEN -e GH_TOKEN \
  <ssh-agent mount> \
  "ghcr.io/<owner>/sealedstack:${version}" "$@"
```

Then:

```sh
bin/sealedstack validate     # schema-check config/ and the compose rules
bin/sealedstack plan         # render + plan the edge and platform stacks
bin/sealedstack apply        # apply them
bin/sealedstack doctor       # check tokens, permissions and the zone
```

The wrapper passes `BWS_ACCESS_TOKEN` and `GH_TOKEN` through from your shell and
mounts the SSH agent (see [spec.md](spec.md) for macOS/Bitwarden notes).

## `sealedstack.version`

The pinned version — the **only** place the tool version is set. Its contents
are the image tag (`0.5.0`), so the wrapper runs
`ghcr.io/<owner>/sealedstack:0.5.0`. To upgrade, change this one line in a pull
request; CI `plan` then shows the impact. That keeps upgrades reviewable and
rollback-able without touching any other file.

## `.sealedstack/`

All generated and derived files live under `/infra/.sealedstack/` (gitignored,
mode `0700`): the rendered manifests, plan/apply scratch output, the effective
Tailscale policy, and OpenTofu working data. Nothing is written inside the
image, because the image's code directory is read-only. The CLI points
`<code>/.build` at `.sealedstack/.build` so scripts that emit build output write
into your repo, not the image.

## Upgrading sealedstack

1. Edit `sealedstack.version` to the new version in a PR.
2. CI runs `sealedstack plan` against the new image and shows what changes.
3. Merge, then run `bin/sealedstack apply` locally.

`sealedstack upgrade-check` will list breaking changes between versions from the
release notes.

## CI

`.github/workflows/sealedstack.yml` calls the reusable workflow
`<owner>/sealedstack/.github/workflows/infra.yml@v<version>`. Pull requests run
`plan`; `apply`/`up` wait for approval in the `production` environment. Comment
commands (`/sealedstack …`) are accepted from writers only.

`check` fails when `sealedstack.version` and the workflow `@ref` differ; the
upgrade bot bumps both in one PR.

## Where to next

- [Getting started](getting-started.md) — accounts, credentials and the first run.
- [Config repository](config-repo.md) — the `SEALEDSTACK_CONFIG` contract.
- [Design spec](spec.md) — the architecture and contracts.
