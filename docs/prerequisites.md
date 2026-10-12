# Prerequisites

Everything below must be in place before `just server-up` and `just apply`.
Adapted from [spec.md §14](spec.md#14-prerequisites).

## Accounts and access

| Requirement | Mandatory | Notes |
|---|---|---|
| Domain on Cloudflare nameservers | ✅ | Tunnel hostnames and Access require a Cloudflare zone. |
| Cloudflare account, Free plan, Zero Trust enabled | ✅ | Zero Trust and R2 may ask for a payment method even on free plans. |
| Cloudflare API token | ✅ | Permissions below. |
| Tailscale account | ✅ | API access token (or OAuth client) with ACL and auth-key scopes; MagicDNS on. |
| Bitwarden account + free organisation with Secrets Manager | ✅ | One machine-account token. Give it **read-only** access to the `infra` project only: it is the only secret CI holds (see [limitations](limitations.md#approvals-on-github-free-private-repositories-what-they-do-and-dont-protect)). |
| GitHub account with `gh` authenticated | ✅ | App repos and `just render`. |
| A server: Ubuntu 24.04 or Debian 12 | ✅ | Control: ≥ 2 vCPU / 4 GB / 40 GB. Worker: ≥ 1 vCPU / 2 GB. |
| Docker on the laptop | ✅ | Everything else runs in the toolbox image. |
| `just` on the laptop | ✅ | `brew install just` (or `cargo install just`). |
| Clerk account | Only for `user_auth: clerk` apps | |

## Environment variables

`just` passes these into the toolbox. Export them in your shell:

| Variable | When | Notes |
|---|---|---|
| `BWS_ACCESS_TOKEN` | always | Bitwarden machine-account token (the only secret you handle directly). |
| `GH_TOKEN` | render/validate with repo apps | So `gh` can fetch `sealedstack.yaml` from app repos. |
| `config/stack.yaml: admin.ssh_keys` | setup | Your SSH public key(s), installed on every server (not a secret, D-040). |
| `ROOT_PASSWORD_<NAME>` | first bootstrap of an existing server | Provider root password (D-040). Delete after bootstrap. |
| `SERVER_IP_<NAME>` | first bootstrap of an existing server | Public IP, kept out of git and job inputs (D-049). Delete after bootstrap. |
| `BACKUP_ENDPOINT` / `BACKUP_REGION` | backup s3-family | Or set the `backup:` block in `stack.yaml`. |
| `SSM_PREFIX` / `SSM_REGION` | secrets: aws_ssm | Defaults are fine. |

## Cloudflare API token permissions

Account scope:

- Cloudflare Tunnel — Edit
- Access: Apps and Policies — Edit
- Workers R2 Storage — Edit

Zone scope (`example.com`):

- DNS — Edit
- Zone WAF — Edit
- Bot Management — Edit
- Single Redirect — Edit

## Bitwarden Secrets Manager layout

| Project | Keys |
|---|---|
| `infra` | `CLOUDFLARE_API_TOKEN`, `TAILSCALE_OAUTH_CLIENT_ID`, `TAILSCALE_OAUTH_CLIENT_SECRET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `state_passphrase`, `DOKPLOY_API_KEY`, `DOKPLOY_SSH_PRIVATE_KEY`, `DOKPLOY_SSH_PUBLIC_KEY`, `admin_sudo_password`, plus provider tokens in use (`HCLOUD_TOKEN`, `DIGITALOCEAN_TOKEN`, `VULTR_API_KEY`, `HOSTINGER_TOKEN`, `NETCUP_API_USERNAME`, `NETCUP_API_PASSWORD`) |
| `infra` (per provider in use) | `HCLOUD_TOKEN`, `HOSTINGER_TOKEN`, `NETCUP_API_USERNAME`, `NETCUP_API_PASSWORD` |
| `prod` | `<APP>.<NAME>` for production apps, e.g. `shop.CLERK_SECRET_KEY` |
| `staging` | Same naming, for staging (v0.2+) |

`DOKPLOY_SSH_*` is generated automatically by `just server-up` on first run.

The only secret the operator handles directly is `BWS_ACCESS_TOKEN`; export it in
the shell and `just` passes it into the toolbox. Any of the keys above may also be
supplied as an environment variable of the same name instead of using Bitwarden.

## config/ files

- `config/stack.yaml` — slots, Cloudflare account/zone/team, gate policies, edge
  security, state bucket.
- `config/servers.yaml` — server name (also its Tailscale hostname), provider,
  role (`control` or `worker`) and OS.
- `config/apps.yaml` — per-app source, servers, hostnames, access.

Run `just validate` after any change.

## Private app repos and the drift job

- **Private app repos (H7):** if an app's `source.repo` is private, Dokploy must
  be able to clone it. Create a **GitHub App** in the Dokploy UI (Settings → Git)
  once and reference its id from the app source, or use a per-repo deploy key
  stored in the secrets slot. Public repos clone over HTTPS with no credentials.
- **Drift job:** the scheduled `drift` workflow needs read access to `infra`
  (Cloudflare, Tailscale, state passphrase). Use a **separate** machine account
  for it — the app-CI account only reads `prod` (spec §6).
