# Staging environments

A staging environment runs the same kind of sealed origin — same mesh, same
edge pattern, same platform — with **`staging.` hostnames** and its **own
secrets**. It exists so you can test a release, a migration or an
infrastructure change against production-like wiring without touching
production hostnames or data.

The model is deliberately thin: staging is a **second config set**, not a
second product. The same modules and roles run; only the values differ.

## What differs from production

| Concern | Production | Staging |
|---|---|---|
| Hostnames | `<app>.<domain>` | `staging.<app>.<domain>` |
| Secrets project | Bitwarden `prod` (`<app>.<NAME>`) | Bitwarden `staging` (`<app>.<NAME>`) |
| Config set | `config/` | a staging config set (see below) |
| State | `sealedstack-state` bucket | a separate key/prefix or bucket |
| Databases | production data | restored from the latest production backup on create |

Nothing else changes: the same `slots:` (ingress, gate, platform, …), the same
playbook, the same modules.

## Hostnames

Prefix every staging hostname with `staging.`:

```yaml
# config/apps.yaml (staging set)
apps:
  shop:
    source: { repo: you/shop, ref: staging }   # the branch under review
    servers: [web-1]
    hostnames: [staging.shop.example.com]
    access: owner-only                          # keep staging gated
```

The edge stack gives each hostname the same treatment as production: a tunnel
or endpoint, DNS, and (when `access` is not `public`) an Access policy. Because
the hostname is distinct, `catch-all` rules on `*.<domain>` still apply, but
staging never shadows a production name.

`just render` merges `config/apps.yaml` with each app's `sealedstack.yaml` as
usual, so a staging entry needs no new schema.

## Secrets: the `staging` project

Secrets live in the Bitwarden **`staging`** project (spec section 6), named
identically to production:

| Project | Holds |
|---|---|
| `infra` | Cloudflare/Tailscale tokens, Dokploy key, state credentials |
| `prod` | `<app>.<NAME>` for production |
| `staging` | `<app>.<NAME>` for staging |

The `laptop` machine account already has read/write on `staging`, so the
operator can seed it before the first apply. Tunnel tokens that OpenTofu
creates (`tunnel.<app>`) are written to the selected project, not hardcoded to
`infra`.

> **Today** `sealedstack.edge` and `sealedstack.buildenv` read the `infra`/`prod`
> projects by name. Running staging for real means making the project name
> configurable (a `sealedstack` environment selector / `--env` flag) so the
> staging set reads `staging`. Until then, export the values in the shell — the
> helpers fall back to same-named environment variables.

## A second config set

`config/` is the only user-specific directory (spec section 5), so staging is a
second copy of it. Pick one:

1. **A separate checkout/branch** — clone the repo, keep `main` for production
   and a `staging` branch whose `config/` holds the `staging.` hostnames. The
   `.build/` snapshot and state stay per-checkout, so `just plan` never mixes
   the two. This is the simplest and the recommended default.
2. **An overlay directory** — keep one checkout and select the config set with
   an environment variable that `engine/sealedstack/secrets.py` honours. This needs a small
   change to the loader (the paths are currently fixed to `config/`).

Either way the two sets never share a `state:` bucket or a Dokploy project:
staging apps deploy as distinct projects (`shop-staging`) on the same panel
(D-010) or on their own server.

## Running a staging apply

From the staging config set:

```sh
just validate            # schema-check the staging config
just render              # merge manifests (use ref: staging in apps.yaml)
just plan                # edge + platform, staging state
just apply               # edge + platform
```

The same `just` recipes work unchanged; they operate on whatever `config/`
they find.

## Promoting to production

Promotion is a human decision and a one-line change: retag the release, point
the production `apps.yaml` at it, and `just apply`. There is no shared
database — staging restores from a backup, production keeps its own. A
`just promote <app> staging → prod` helper is planned (see
roadmap, Operations).

## Related

- [operations.md](operations.md) — staging status and the rest of the ops list.
- [preview-environments.md](preview-environments.md) — per-PR environments,
  the ephemeral sibling of staging.
- [spec.md §6](spec.md#6-secrets-model) — the Secrets Manager layout.
