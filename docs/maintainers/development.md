# Development and repo tooling

> **Maintainers.** This page explains how sealedstack is built and why. It is **not** instructions for
> setting up or running a sealedstack infrastructure, and parts of it describe designs that were later
> changed (the decision log says which). To use sealedstack, start at [docs/](../README.md).


sealedstack is mostly YAML, HCL and shell. The tooling here is deliberately
small; this page explains every non-obvious file so a newcomer (or agent) is not
surprised by it.

## Dotfiles at the repo root

| File | What it is | Why it's here |
|---|---|---|
| `.gitignore` | files never committed | build output (`.build/`), tofu state/backend, `config/secrets/`, caches |
| `.dockerignore` (in `toolbox/`) | build context filter | keeps the toolbox image small |
| `.ansible-lint` | ansible-lint config | lint the playbooks/roles in CI |
| `.pre-commit-config.yaml` | pre-commit hooks | run gitleaks + `tofu fmt` + YAML checks locally before commit |
| `renovate.json` | Renovate bot config | opens PRs when pinned tools/providers/images go stale |
| `.github/workflows/ci.yml` | CI | config+registry check, `tofu fmt`/`validate`, ansible-lint, gitleaks |
| `.github/workflows/drift.yml` | scheduled `tofu plan` | detects drift against the real accounts (needs repo secrets) |
| `LICENSE` | Apache-2.0 | the open-source licence |

These are standard "keep the repo healthy" tools. If you don't use GitHub,
delete `.github/`; if you don't use Renovate, delete `renovate.json`. Nothing in
the runtime path depends on them.

## Layout

```
justfile            single entrypoint
toolbox/            the Docker image every command runs in
config/             the only user-specific values (+ *.example.yaml samples)
registry/           provider catalog, one file per slot (the source of truth)
schemas/            JSON Schemas for config + registry
scripts/            shell + python glue (render, validate, doctor, providers, ...)
ansible/            host roles and playbooks
tofu/modules/       provider implementations (slot_impl)
tofu/stacks/        state | edge | platform | compute (compose the modules)
examples/           app template (manifest + compose + release CI)
docs/               spec, decisions, plans, guides
```

## Local workflow

```sh
just validate        # the main gate; run before every commit
just providers       # browse the registry
just doctor          # what's missing for your selected providers
pre-commit install   # optional: run gitleaks/tofu-fmt on commit
```

CI runs the same checks. `just validate` requires a rendered `.build/`; the CI
job and `just plan/apply` run `scripts/render.py` first (needs `GH_TOKEN` for
repo apps, but tolerates unresolved manifests).

### Running locally needs a config

The repo carries **no config** (`config/*.yaml` are gitignored, D-022); only the
`*.example.yaml` samples are committed. `just validate`, `just plan` and
`just apply` all need a real config, so on a fresh checkout create one first:

```sh
cp config/stack.example.yaml   config/stack.yaml
cp config/servers.example.yaml config/servers.yaml
cp config/apps.example.yaml    config/apps.yaml
# ...or: just init
```

To keep the config in a separate (private) repo, point `SEALEDSTACK_CONFIG` at
that directory instead of copying into `config/`:

```sh
export SEALEDSTACK_CONFIG=/absolute/path/to/your-config
just validate
```

`SEALEDSTACK_CONFIG` wins when set; otherwise scripts fall back to
`<repo>/config`. See [config-repo.md](../config-repo.md) and
[limitations.md](../limitations.md).

## Pinned installers on the servers (DR-8)

Root on a new server never runs a live installer script. Two pins live in the
Ansible roles and are bumped by a maintainer in a reviewed PR:

- **Tailscale** comes from its signed apt repository. The repository key's
  fingerprint is pinned in `ansible/roles/mesh_tailscale/defaults/main.yml`
  (`mesh_tailscale_apt_signer_fingerprint`); the install tasks refuse any other key.
  It only changes if Tailscale rotates its signing key; the file lists the
  commands to re-verify it.
- **Dokploy** is installed by its `install.sh`, fetched from an immutable git
  commit of the Dokploy website repository (the script is not part of the panel's
  release tags) and checked against `dokploy_installer_sha256` before it runs.
  Renovate bumps `dokploy_version` in `ansible/roles/platform_dokploy/defaults/main.yml`;
  when it does, pick the installer commit that was current at that release,
  record its SHA-256 and read the diff against the old installer (the three
  steps are in the defaults file). `tests/pytest/test_ansible_installers.py`
  fails if any `curl | sh` appears under `ansible/`.

The Coolify role (not on the golden path) refuses to run without a recorded
`platform_coolify_install_sha256`.

## Cutting a release

From a checkout rebased onto `origin/main`:

```
scripts/release.sh --dry-run    # the next version and the commits that would ship
scripts/release.sh              # bump, test, push main and the tag, dispatch the release, wait
```

`--minor` or `--version X.Y.Z` override the next patch number; `--yes` skips the confirmation. See D-080.
