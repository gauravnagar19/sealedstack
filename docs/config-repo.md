# Keeping config in a separate repository

`config/` holds your servers, domains, app placement and provider settings — the
only user-specific files in sealedstack. It is kept separate so the generic code
can be public while your topology stays private (D-022). In the normal CI-first
setup this is already the case: `config/` lives in your private infra repo, and
the engine reads it through `SEALEDSTACK_CONFIG=/infra/config`. This page explains
the variable for the other layouts.

Every script resolves the config directory from a single source of truth:

```
SEALEDSTACK_CONFIG   # the config directory
```

When the variable is unset, it defaults to `<repo>/config`, so a single-repo
checkout keeps working with no changes. When it is set, that directory is read
and written instead. Nothing else (schemas, registry, tofu, ansible roles) moves.

## Which files travel

The config directory contains the three entry files and the provider overrides:

```
config/
├── stack.yaml          # domain, admin_user, slots, cloudflare, gate, state
├── servers.yaml        # one control server + workers (D-010)
├── apps.yaml           # app placement: source, servers, hostnames, access
└── providers/          # per-provider non-secret overrides
```

`config/secrets/` (SOPS ciphertext, if you use the `sops` secrets slot) and the
repo-level `internal/` directory stay **untracked** — `.gitignore` covers both.
Secrets otherwise live in your secrets manager (Bitwarden by default, D-008), so
only non-secret config belongs in the config repo.

## Option A — a sibling `sealedstack-config` repo

Create a private repo that holds your `config/` contents at its root, then point
sealedstack at it:

```sh
git clone git@github.com:you/sealedstack-config.git ../sealedstack-config
export SEALEDSTACK_CONFIG="$(pwd)/../sealedstack-config"   # absolute path is clearest
sealedstack check
```

Keep the export out of committed files.

## Option B — a git submodule at `config/`

If you prefer the files to sit at the conventional path, add the private repo as
a submodule:

```sh
git submodule add git@github.com:you/sealedstack-config.git config
```

The default `<repo>/config` then points at the submodule, so no environment
variable is needed.

## Option C — any path

`SEALEDSTACK_CONFIG` can point anywhere — an encrypted volume, a mount, another
checkout:

```sh
SEALEDSTACK_CONFIG=/mnt/ss-config sealedstack plan
```

`sealedstack setup` writes to the resolved directory and creates it if needed;
pass `--config-dir` to override the environment for a single run:

```sh
python3 scripts/init.py --config-dir ../sealedstack-config
```

## Verify

```sh
SEALEDSTACK_CONFIG=/path/to/config sealedstack check
```

The paths printed by `check` and `debug render` are informational (`config/...`), but
the file reads and writes come from `SEALEDSTACK_CONFIG`.
