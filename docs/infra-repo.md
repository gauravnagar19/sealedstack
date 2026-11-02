# The infra repo

sealedstack is not a repo you clone and fill in. It is a **versioned tool image
with a CLI** (D-031). Each user keeps their own **infra repo**: a small, private
repository that holds *configuration* and *pins a sealedstack version*. The
sealedstack source is never copied in. The engine runs in **your own CI** from a
pinned image, so you install nothing (D-034).

This page describes that model. For the design, see
[design spec, for maintainers](maintainers/spec.md); for the
commands, see [getting-started.md](getting-started.md).

## Three repositories

```
┌──────────────────────────────┐  pins version   ┌───────────────────────────────┐
│ sealedstack  (the tool)      │◄────────────────│ <name>-infra  (user's config) │
│ public · versioned · image:  │                 │ private · your choice of name │
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

## One infra repo per cluster

Keep **one infra repo for each group of servers and domain that run together**. Apps
that share those servers belong in the same infra repo, listed in its `apps.yaml`:
they share the Tailscale policy, the state bucket and the deploy pipeline. Make a new
infra repo only for a separate set of servers or a separate domain.

## Layout

You create the repo (any name, e.g. `my-infra`, D-028) and commit two files, then
the **Setup** job proposes the rest as a pull request
([getting started](getting-started.md)). The result looks like this:

```
my-infra/
├── sealedstack.version            # e.g. "0.5.0" — the only place the tool version is set
├── .devcontainer/devcontainer.json    # a Codespace pinned to the engine image (D-046)
├── bin/sealedstack                # optional launcher (generated): docker run the pinned image
├── config/
│   ├── stack.yaml                 # domain, slots, cloudflare, gate, edge_security, mesh, registry, state
│   ├── servers.yaml               # server-1 …
│   ├── apps.yaml                  # placement + exposure per app
│   ├── tailscale-policy.hujson    # YOUR tailnet policy, imported by `check` (D-026)
│   └── providers/                 # optional per-provider non-secret overrides
├── .github/workflows/sealedstack.yml  # calls the reusable infra.yml (plan on PRs; writes only from merged, approved `main`)
├── .gitignore                     # .sealedstack/, *.tfstate*
└── README.md                      # generated: what this repo is, how to run it
```

## Running the tool

**In CI (the normal way).** The workflow in `.github/workflows/sealedstack.yml`
runs the pinned image on a GitHub runner. You start commands with **Actions, Run
workflow**, or with `/sealedstack <command>` comments on a pull request (writers
only). The commands and approvals are in the
[user experience](user-experience.md#121-commands-ci-input-pr-comment-and-local-all-the-same).

**In a Codespace (optional).** `.devcontainer/devcontainer.json` points at the
pinned image, so a Codespace gives you a terminal with `sealedstack` ready and
nothing to install. Locally the commands are read-only (`check`, `plan`,
`status`); `apply`, `up`, `server add` and `db restore` are refused unless
`--break-glass` is given, and then only when the checkout equals `origin/main`
(D-045).

**The launcher (optional, for power users and break-glass).** If you have a
Docker-compatible runtime, the generated `bin/sealedstack` runs the same pinned
image against your checkout. It writes files only; it never commits or pushes
(D-043):

```bash
#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
version="$(cat "$here/sealedstack.version")"
exec docker run --rm -i $( [ -t 0 ] && echo -t ) \
  -v "$here:/infra" -w /infra \
  -e SEALEDSTACK_CONFIG=/infra/config -e SEALEDSTACK_WORKDIR=/infra/.sealedstack \
  -e BWS_ACCESS_TOKEN -e GH_TOKEN \
  "ghcr.io/<owner>/sealedstack:${version}" "$@"
```

```sh
bin/sealedstack check        # preflight, read-only
bin/sealedstack plan         # render + plan the edge and platform stacks, read-only
bin/sealedstack status       # health, versions, backups, drift, read-only
```

## `sealedstack.version`

The pinned version — the **only** place the tool version is set. Its contents
are the image tag (`0.5.0`), so CI (and the launcher) runs
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

1. A bot opens a pull request that bumps `sealedstack.version` **and** the workflow
   ref together, with the release notes and the plan under the new version.
2. Review it (the plan is normally "0 changes") and merge.
3. If the plan has changes, an approver approves the resulting `apply` as usual.

Nothing upgrades on its own
([journey F](user-experience.md#9-journey-f-upgrade-sealedstack)).

## CI

`.github/workflows/sealedstack.yml` calls the reusable workflow
`<owner>/sealedstack/.github/workflows/infra.yml@v<version>`. When no command is
given, the command comes from the event:

| Event | What runs |
|---|---|
| Pull request opened or updated | `plan`, posted (and updated in place) as a PR comment; on the Setup PR, `check` updates the checklist |
| Push to `main` (a merge) | `apply`. If no approver has agreed yet it posts "waiting for `/sealedstack approve`" on the PR and exits successfully; a direct push is refused (D-052) |
| Schedule (hourly) | `status` (updates the Status issue); then drift detection, a problem becoming a Status-issue comment, only when the repository variable `SEALEDSTACK_DRIFT_ENABLED` is `true` (D-059) |
| Comment `/sealedstack <cmd>` | Writers can run `check`, `plan`, `status`, `drill`, `drift`; approvers can run `up`, `apply` and `approve`. After a merge, `approve` triggers the `apply` |
| Run workflow | The chosen command, including `plan`, `apply` and `drift` (a read-only check, no approval) |

Write commands run only from a merged pull request on `main` and after an
approver has agreed (D-052). Every job that reaches a server or Dokploy first
joins the tailnet as `tag:ci` (workload identity first, the OAuth client as a
logged fallback; D-041). App repos' release jobs use their own tag, `tag:release`,
which reaches only the Dokploy API (D-057).

`check` fails when `sealedstack.version` and the workflow `@ref` differ; the
upgrade bot bumps both in one PR.

## Where to next

- [Getting started](getting-started.md): the first setup, from an empty repo.
- [Config repository](config-repo.md) — the `SEALEDSTACK_CONFIG` contract.
- [Design spec (for maintainers)](maintainers/spec.md) — the architecture and contracts.
