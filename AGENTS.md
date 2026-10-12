# AGENTS.md

Guidance for AI agents (and humans) working **on** or **with** sealedstack.

## What this project is

sealedstack turns an empty Linux box into a **sealed origin**: no inbound ports,
admin access only over a private mesh (Tailscale by default), apps served through
an outbound tunnel (Cloudflare Tunnel / ngrok) with a gate in front, everything
rebuildable from git + a secrets manager + object storage.

Four layers, one tool each:

| Layer | Scope | Tool |
|---|---|---|
| Host | OS, users, SSH, mesh, firewall, Docker, Dokploy | Ansible (`ansible/`) |
| Edge | tunnels, DNS, gate, WAF, mesh ACL, R2 | OpenTofu (`tofu/stacks/edge`) |
| Platform | Dokploy projects, composes, domains, DBs, backups, connectors | OpenTofu (`tofu/stacks/platform`) |
| Release | building/deploying an app version | the app repo's CI (`ci` slot) |

Everything the user runs goes through `just`, which executes inside the toolbox
Docker image (`toolbox/Dockerfile`). Read `docs/spec.md` for the design and
`README.md` for usage.

## Repository layout

```
justfile            single entrypoint
toolbox/            the Docker image all commands run in
config/             the only user-specific values (+ *.example.yaml samples)
registry/           provider catalog, one file per slot (source of truth)
schemas/            JSON Schemas for config + registry
scripts/            shell + python glue (render, validate, doctor, providers)
ansible/            host roles + playbooks
tofu/modules/       provider implementations (<slot>_<impl>)
tofu/stacks/        state | edge | platform | compute
examples/           app template (manifest + compose + CI)
docs/               user + design docs (see docs/README.md)
```

## How to work here

- Run commands through `just` (they run in the toolbox): `just validate`,
  `just providers`, `just doctor`, `just plan`, `just apply`.
- Never commit secrets. `config/` holds no secrets (D-008); secrets live in
  Bitwarden and are read via `engine/sealedstack/secrets.py`.
- Never hardcode user-specific values (admin user, domains, tokens). They come
  from `config/stack.yaml` (`admin_user`) and the registry.
- Validation gate before you call anything done:

  ```sh
  python3 scripts/registry.py check
  python3 scripts/render.py --allow-unresolved && python3 scripts/validate.py
  tofu fmt -check -recursive tofu
  for s in state edge platform compute; do
    tofu -chdir=tofu/stacks/$s init -backend=false >/dev/null && \
    tofu -chdir=tofu/stacks/$s validate
  done
  ansible-lint
  ```

## The provider registry (how to extend)

Every slot (compute, mesh, ingress, gate, secrets, backup, platform, user_auth,
ci, registry, forge) has a catalog at `registry/<slot>.yaml`, validated by
`schemas/registry.schema.json`. `just providers [slot]` lists it; `just validate`
fails if a **selected** implementation is missing or `planned`; `just doctor`
checks its `secrets`.

An entry:

```yaml
- id: coolify
  name: Coolify
  status: planned            # supported | experimental | planned
  creates_server: false      # compute only
  summary: Self-hosted PaaS.
  secrets: [COOLIFY_API_TOKEN]
  config: [endpoint]
  impl: { tofu_module: platform_coolify, stack: platform }
  docs: https://coolify.io/
```

### Convention rules

1. IDs match `^[a-z0-9][a-z0-9_-]*$`.
2. `impl` points at real code: `tofu_module` (dir under `tofu/modules/`),
   `ansible_role` (dir under `ansible/roles/`), `adapter` (file under `engine/sealedstack/`).
3. `secrets` lists exact secret names (env var name == Bitwarden key name).
4. Same-slot implementations share the **contract** (the `contract:` line in the
   registry file) and matching inputs/outputs:
   - compute modules output `{ ip, managed, firewall }`.
   - secrets modules take `{ keys (list, non-sensitive), values (map, sensitive) }`.
   - backup modules output `{ bucket, endpoint }`.
5. No secrets or user-specific values in committed files.

### Recipes

- **native** — add a module `tofu/modules/<slot>_<impl>/` (copy the closest
  sibling), pin the provider in the owning stack's `versions.tf`, wire it into
  the stack (`stacks/compute|edge|platform`).
- **ansible** — add a role `ansible/roles/<slot>_<impl>/`, add it to
  `ansible/playbooks/site.yml` gated by `slots.<slot>`.
- **adapter** — add `engine/sealedstack/<name>.py` reading `config/providers/<slot>_<impl>.yaml`
  or the `providers:` block; see `engine/sealedstack/firewall.py` (REST) and
  `engine/sealedstack/sopssecrets.py` (CLI).
- **config** — env/records only; the `user_auth` `oidc` entry is the template.

### When an upstream provider/API changes

1. Bump the pin in `tofu/stacks/*/versions.tf` (Renovate also does this), then
   `tofu init -upgrade` + `tofu validate`.
2. Re-check the schema the module uses: `tofu providers schema -json` and diff
   the resource/attribute names against the module.
3. For REST adapters, update `config/providers/<slot>_<impl>.yaml` (paths, auth,
   body template) — this is the point of adapters: no code change.
4. Update the registry `notes:` and re-run the validation gate.

## Definition of done (per provider)

1. Registry entry. 2. Code implementing the contract. 3. Wired so selecting it in
`config/stack.yaml` (or per-server `provider`) works. 4. Sample/documented keys +
secret names. 5. The validation gate above passes.

## Internal planning

Detailed build plans and the long-term roadmap live **outside** this repository
(internal, not published). Do not add planning docs to `docs/`; keep `docs/` for
user and design documentation (see `docs/README.md`).
