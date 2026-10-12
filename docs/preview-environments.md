# Preview environments per pull request

A preview (review) environment is a throwaway deployment of a pull request at
its own hostname, so a change can be reviewed against a real URL before it is
merged.

**Target shape:** `"<pr>-<app>.<domain>"`, for example `128-shop.example.com`.
Created when the PR opens, refreshed on each push, destroyed on merge or close.
The database is cloned from the latest backup; the hostname is gated like any
other app.

> **Status:** planned (see roadmap, Operations). This page records
> the approach and the seam; it is not implemented yet.

## How it fits sealedstack

Preview environments reuse the existing layers instead of adding one:

- **Config is the source of truth.** An app is declared in `config/apps.yaml`
  with `source.repo`/`source.ref` and its `hostnames`. A preview is the same
  app entry with a per-PR `ref` and a per-PR hostname.
- **The platform slot deploys compose services.** `tofu/stacks/platform` and
  `tofu/modules/platform_dokploy` render one compose deployment per app ×
  server, with domains, env vars and (optionally) a managed database.
- **The edge slot publishes the hostname.** The ingress contract is
  `app, hostnames[], servers[] → per-server connector token + DNS records`, so a
  new hostname is a new tunnel/endpoint plus DNS — no new machinery.
- **The gate protects it.** A preview is typically `access: owner-only`, so
  unfinished work is never public.

Because `.build/` is a rendered snapshot of `config/` (spec section 5.5), a
preview plan is reproducible and never reads a live manifest mid-run.

## Approach

Two seams, in order of least effort:

1. **Platform-native previews.** Most PaaS platforms — including Dokploy —
   support *preview deployments* tied to a branch or PR: a second compose
   service built from the branch, with its own domain. Let the platform own the
   lifecycle and the clone; sealedstack owns the hostname and the gate. See
   [Dokploy preview support](https://docs.dokploy.com/docs/core/applications)
   (Applications → deployments/branches) and the `platform` slot in
   [providers.md](providers.md).
2. **CI-driven, tofu-rendered.** A GitHub Actions job on `pull_request` writes
   a temporary app entry (`<pr>-<app>.<domain>`, `ref: <head-sha>`), runs
   `just render && just apply`, and on `pull_request: closed` runs a destroy.
   This keeps one code path (the normal stacks) at the cost of a plan/apply per
   PR.

Either way the per-PR database should be restored from the newest production
backup rather than seeded from scratch:

```sh
just app-restore shop                 # latest backup, into the preview DB
```

## What each provider needs

Preview support depends on the **platform** slot (does it know about branches
and preview domains?) and, for the URL, on the **ingress** slot (can it create
a hostname on demand?). Provider-specific links:

- Dokploy — [applications and deployments](https://docs.dokploy.com/docs/core/applications)
  (branch/commit deploys; use a distinct project per preview).
- Coolify — [preview deployments](https://coolify.io/docs/applications/) (PR
  previews are a first-class feature).
- Cloudflare Tunnel (ingress) — one tunnel per app, so a preview needs its own
  short-lived tunnel and proxied CNAME.
- ngrok (ingress) — a reserved domain plus a cloud endpoint per hostname, or a
  random agent URL for very short-lived previews.

## What is needed to ship it

1. A `source.ref` that can be a commit SHA (already modelled) and a
   `<pr>-<app>` naming convention for projects and hostnames.
2. A tear-down path: destroy the compose deployment, the DB, the tunnel and the
   DNS record together (today `just apply` is additive; previews need a
   scoped `destroy`).
3. CI wiring: a `pull_request` workflow that renders, applies and, on close,
   destroys — the mirror of `.github/workflows/drift.yml`.
4. Optional: a `just preview <app> <pr>` command that does the above locally.

## Related

- [staging.md](staging.md) — the long-lived, non-ephemeral sibling.
  (`<pr>-<app>.<domain>`, DB clone, `just promote`).
- [providers.md](providers.md) — the `platform` and `ingress` slots.
