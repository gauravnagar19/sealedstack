# Operations, scaling and hardening

Status of the roadmap items that are operational or scaling related. "Done"
means it is implemented in this repo; "planned" means the contract is recorded
but the work is not done.

## Data and scaling

| Item | Status | Notes |
|---|---|---|
| Redis / Valkey under `needs:` | done | `needs.redis` -> `dokploy_redis` + `REDIS_URL`. |
| Managed Postgres option (Neon) | done | `needs.postgres.provider: neon` reads the connection string from `.build/env.json` and injects it; the platform creates no `dokploy_postgres`. |
| Object storage for uploads under `needs:` | done | One R2 bucket per app + S3 env vars; scoped tokens are manual ([Object storage](#object-storage-needsobject_storage)). |
| Database HA / streaming replication | planned | Dokploy's Postgres is single-node. |
| Read replicas | planned | Not offered by Dokploy-managed Postgres. |
| Moving an app between servers with its DB | planned | Manual: back up, change `servers:`, `just app-restore`. |
| Multiple Dokploy panels / panel HA | planned | One panel by design (D-010); the panel is rebuildable from code. |

## Object storage (`needs.object_storage`)

An app that declares `needs.object_storage` in its `sealedstack.yaml` manifest
gets a Cloudflare R2 bucket and S3 credentials injected as environment
variables. The platform stack (`tofu/stacks/platform`) owns the bucket; the
credentials are read from the environment/secrets (below).

```yaml
# sealedstack.yaml in the app repo
needs:
  object_storage:
    env: STORAGE              # required: env-var prefix
    bucket: sealedstack-shop # optional; defaults to sealedstack-<app>
    region: auto              # optional; R2 uses "auto"
    prefix: uploads/          # optional; injected as STORAGE_PREFIX
```

The platform creates `cloudflare_r2_bucket` and injects into the app's compose
environment:

| Env var | Value |
|---|---|
| `<env>_ENDPOINT` | `https://<account_id>.r2.cloudflarestorage.com` |
| `<env>_REGION` | `region` (default `auto`) |
| `<env>_BUCKET` | bucket name |
| `<env>_ACCESS_KEY_ID` | S3 access key id |
| `<env>_SECRET_ACCESS_KEY` | S3 secret access key |
| `<env>_PREFIX` | `prefix`, only when set |

### Configuration

| Input | Source | Fallback |
|---|---|---|
| Cloudflare API token (R2 edit) | `TF_VAR_cloudflare_api_token` | `CLOUDFLARE_API_TOKEN` env (passed into the toolbox by `just`) |
| Cloudflare account id | `TF_VAR_cloudflare_account_id` | `cloudflare.account_id` in `config/stack.yaml` |
| S3 access key id | `TF_VAR_object_storage_access_key_id` | `TF_VAR_backup_access_key_id` (the R2 key already used for backups) |
| S3 secret access key | `TF_VAR_object_storage_secret_access_key` | `TF_VAR_backup_secret_access_key` |

`just plan`/`just apply` run `sealedstack.platform`, which already exports the
backup R2 credentials from Secrets Manager (`infra.R2_ACCESS_KEY_ID` /
`infra.R2_SECRET_ACCESS_KEY`), so an app reuses them with no extra setup. To use
a **dedicated** key, export `TF_VAR_object_storage_access_key_id` and
`TF_VAR_object_storage_secret_access_key` (e.g. add them to `sealedstack.platform`)
before applying.

If an app declares `needs.object_storage` and the account id or S3 credentials
are empty, the platform fails at plan time instead of injecting blank values.

### Manual steps

- **Scoped R2 tokens.** OpenTofu (and the Cloudflare provider) cannot mint R2 S3
  API tokens, so the key above is account-scoped. To follow least privilege,
  create one token per app bucket in the Cloudflare dashboard
  (**R2 → Manage API Tokens → Create**, permission *Object Read & Write*, scoped
  to the app's bucket) and supply it via the `TF_VAR_object_storage_*` variables.
  A provider that manages R2 tokens is a tracked follow-up (spec Q1).
- **Bucket deletion.** R2 buckets must be empty before they can be deleted;
  remove the app's objects first if you are tearing an app down.

## Operations

| Item | Status | Notes |
|---|---|---|
| `just doctor` (tokens, tools, registry) | done | `scripts/doctor.py`. |
| Scheduled `tofu plan` drift detection | done | `.github/workflows/drift.yml` (add repo secrets). |
| Renovate for pinned images and tools | done | `renovate.json`. |
| Full DR drill on a schedule | done (manual) | [rebuild-drill.md](rebuild-drill.md); schedule it in your CI. |
| CI for this repo | done | `.github/workflows/ci.yml`: registry, config, tofu fmt/validate, ansible-lint, gitleaks. |
| Monitoring and alerting | done (heartbeat) | `monitoring_healthchecks` role (external uptime heartbeat) plus Dokploy's built-in monitoring; add per-app checks as needed. |
| Central log shipping | planned | Dokploy log rotation only (`max-size=10m`). |
| Staging environments | planned | `staging.` hostnames + the Bitwarden `staging` project. |
| Preview environments per PR | planned | Depends on the platform's preview support. |
| Cost dashboard | planned | — |

## Security hardening

| Item | Status | Notes |
|---|---|---|
| Zero inbound ports + DOCKER-USER | done | firewall role, D-005. |
| Least-privilege Tailscale ACL | done | `tofu/stacks/edge/main.tf` (`locals.tailscale_policy`), D-014. |
| gitleaks | done | CI + pre-commit. |
| Image vulnerability scanning / SBOMs | planned | Belongs in each app's CI. |
| Audit logging (Access logs to storage) | planned | Free plan keeps Access logs 24 h. |
| CIS benchmark baseline role | planned | — |
| CrowdSec | planned | Mostly redundant with zero inbound ports. |
| Hardware key (FIDO2) for SSH and Bitwarden | planned | — |
| Secret rotation | planned | `scripts/` hook point; rotate Cloudflare/Tailscale/DB/tunnel values. |

## Developer experience and open source

| Item | Status | Notes |
|---|---|---|
| `just init` wizard | done | `scripts/init.py`. |
| Split `config/` into `sealedstack-config` | done | `SEALEDSTACK_CONFIG` and `docs/config-repo.md` (D-020). |
| Example app templates | done | `examples/app-template/`. |
| Licence and contribution guide | done (licence) | Apache-2.0 (`LICENSE`). |
| Publish the toolbox image to GHCR | planned | Build from `toolbox/Dockerfile`. |
| Public docs site | planned | The `docs/` markdown is the source. |
