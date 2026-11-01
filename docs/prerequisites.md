# Prerequisites

Everything below must exist before you run `up`. The Setup checklist
([getting started](getting-started.md#3-run-setup)) checks each item for you and
links to the page where you create it. You install **nothing** on your own
machine: the engine runs in your GitHub Actions.

## Accounts and access

| Requirement | Mandatory | Notes |
|---|---|---|
| A domain on Cloudflare nameservers | yes | Tunnel hostnames and Access require a Cloudflare zone. |
| Cloudflare account (Free plan) with Zero Trust enabled | yes | Zero Trust and R2 may ask for a payment method even on free plans. |
| Tailscale account with MagicDNS on | yes | Admin access and server-to-CI access run over the tailnet. |
| A secret source: Bitwarden (a free organisation with Secrets Manager), Doppler or Infisical | yes | Bitwarden is the default; for [Doppler](#using-doppler-instead-of-bitwarden) set `slots.secrets: doppler`, for [Infisical](#using-infisical-instead-of-bitwarden) `slots.secrets: infisical`. sealedstack only reads from it. |
| GitHub account and a **private** infra repository | yes | Runs the engine in GitHub Actions. See [the first steps](getting-started.md#1-create-the-infra-repo). |
| A server: Ubuntu 24.04 or Debian 12 | yes | Control: at least 2 vCPU / 4 GB / 40 GB. Worker: at least 1 vCPU / 2 GB. An existing machine (Netcup, any SSH host) or one a supported provider creates for you. |
| Clerk account | only for apps with `user_auth: clerk` | |

You do **not** need Docker, `just`, OpenTofu, Ansible, Python or `gh` on your own
machine. A browser is enough.

## What you create, and where it goes

The user-created set is deliberately small. Every other value (tunnel tokens,
database passwords, the Dokploy to worker SSH key and so on) is generated and kept
in the encrypted OpenTofu state.

| What | Goes into | Notes |
|---|---|---|
| Bitwarden machine-account token (Doppler: a service token, `DOPPLER_TOKEN`, see [below](#using-doppler-instead-of-bitwarden); Infisical: `INFISICAL_CLIENT_ID` and `INFISICAL_CLIENT_SECRET`, see [below](#using-infisical-instead-of-bitwarden)) | GitHub repository secret `BWS_ACCESS_TOKEN` | The only secret in GitHub. Give the machine account **read-only** access to the secrets project (default `infra`). See [limitations](limitations.md#approvals-on-github-free-private-repositories-what-they-do-and-dont-protect). |
| Cloudflare API token | Bitwarden: `CLOUDFLARE_API_TOKEN` | [Permissions below](#cloudflare-api-token-permissions). |
| Tailscale OAuth client | Bitwarden: `TAILSCALE_OAUTH_CLIENT_ID`, `TAILSCALE_OAUTH_CLIENT_SECRET` | Scopes `policy_file`, `auth_keys`, `devices:core`, with the tag **`tag:server`** and no other. Server auth keys are minted from it, so no long-lived API key is needed; `devices:core` also lets `up` ask the tailnet whether a server is already bootstrapped (without it `up` falls back to whether the bootstrap secrets are still present). |
| Tailscale OAuth client for CI (`TAILSCALE_CI_OAUTH_CLIENT_ID`, `TAILSCALE_CI_OAUTH_CLIENT_SECRET`) | secret source | A **second** client with the scope `auth_keys` and the tag **`tag:ci`** only. Tailscale lets a client mint keys only for exactly its own tag set (a key for just `tag:server` from a `tag:ci` + `tag:server` client is refused), so the client that creates server keys cannot also be the one CI joins the tailnet with. Not needed when CI joins with a workload-identity trust credential instead (`providers.mesh_tailscale.oauth_client_id` and `audience`, see [CI/CD](ci-cd.md)). |
| Registry pull token (only for apps with their own repository: `REGISTRY_PULL_TOKEN`) | secret source | A **classic** GitHub personal access token with only the `read:packages` scope ([create it with the scope pre-selected](https://github.com/settings/tokens/new?scopes=read:packages&description=sealedstack-pull)). The server uses it to pull the private images your app's CI builds. Fine-grained tokens do not work with ghcr.io: GitHub accepts them but every image answers 403. A classic token reads every package of its account, so use a dedicated machine account for anything beyond a personal setup. Not needed when the images are public or the app is image-only. To keep an existing key name, map it with `secrets.keys.registry_pull_token`. `check` probes it (row `registry.pull_token`). |
| Dokploy admin password (optional, `DOKPLOY_ADMIN_PASSWORD`) | secret source | A password you choose for the Dokploy panel, at least 12 characters. Without it sealedstack generates one, keeps it in the encrypted state and shows it once in a local `--break-glass` session. With it, `up` creates the admin with it or changes an existing admin to it, and re-applies it if someone changes it by hand. The key name can be changed with `secrets.keys.dokploy_admin_password`. |
| State passphrase | Bitwarden: `STATE_PASSPHRASE` | At least 16 random characters. Encrypts the OpenTofu state. |
| Existing server: public IP and root password | Bitwarden: `SERVER_IP_<NAME>`, `ROOT_PASSWORD_<NAME>` | `<NAME>` is the server name upper-cased with `-` as `_`, so `server-1` gives `SERVER_IP_SERVER_1`. Used once by the first bootstrap; delete them afterwards. |
| Admin SSH public key(s) | `config/stack.yaml`: `admin.ssh_keys` | A public key, not a secret. Installed on every server at bootstrap. |
| Bot identity | GitHub repository secret `GH_TOKEN` | A **fine-grained personal access token** limited to the infra repo and the app repos ([permissions below](#bot-identity-fine-grained-token)). Its pull requests trigger the plan workflows, which the built-in `GITHUB_TOKEN` cannot ([D-058](maintainers/decisions.md)). |
| Approvers | `config/stack.yaml`: `approvers` | GitHub usernames allowed to approve write commands. Setup defaults it to the repository owner. |

### Key names are declared in config

Setup writes the **name of every secret** into your config, so the mapping from
setting to Bitwarden entry is visible and reviewable ([D-050](maintainers/decisions.md)):

```yaml
# config/stack.yaml
secrets:
  project: infra            # the Bitwarden Secrets Manager project for infra secrets
  app_project: prod         # the project that holds app secrets
  keys:                     # logical name -> key in the secret source
    cloudflare_api_token:          CLOUDFLARE_API_TOKEN
    tailscale_oauth_client_id:     TAILSCALE_OAUTH_CLIENT_ID
    tailscale_oauth_client_secret: TAILSCALE_OAUTH_CLIENT_SECRET
    state_passphrase:              STATE_PASSPHRASE
    bot_private_key:               SEALEDSTACK_BOT_PRIVATE_KEY
```

Rename any value to match an entry you already have (for example
`cloudflare_api_token: MY_CF_TOKEN`).

`project` and `app_project` are Bitwarden concepts. With Doppler they are ignored:
the service token itself picks the config ([below](#using-doppler-instead-of-bitwarden)).
With Infisical they are ignored too: `providers.secrets_infisical` names the project,
environment and folder ([below](#using-infisical-instead-of-bitwarden)).

```yaml
# config/servers.yaml (an existing machine)
servers:
  server-1:
    provider: netcup
    role: control
    os: ubuntu-24.04
    bootstrap:
      method: password                       # the only method for now; `key` is refused (D-061)
      ip_secret: SERVER_IP_SERVER_1          # a key in the secret source, never the IP itself
      password_secret: ROOT_PASSWORD_SERVER_1
```

A logical secret you leave out defaults to its upper-cased name (`hcloud_token`
is read from `HCLOUD_TOKEN`). Error messages and the checklist always show both
names, for example `state_passphrase (source key STATE_PASSPHRASE)`.

Older setups stored the passphrase under the key `state_passphrase`. That key is
still read as a **deprecated fallback** (with a warning in `check`) until you
rename it to `STATE_PASSPHRASE` or point `secrets.keys.state_passphrase` at it.

App secrets default to `<app>.<NAME>` in the app-secrets project
(`secrets.app_project`, default `prod`) and can be remapped per app in
`config/apps.yaml` under `secrets:`. The infra project is `secrets.project`
(default `infra`).

## Other secrets the current implementation reads

These are read from the same Bitwarden project today. They are listed so nothing
surprises you at `up` time. Some are planned to be generated or derived instead
(see the [user experience](user-experience.md) and [decisions](maintainers/decisions.md)
D-044).

| Key | When | Notes |
|---|---|---|
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | optional override | Not needed. The R2 S3 keys are derived from the Cloudflare token (D-054): key id = the token's id, secret = the SHA-256 hex of the token value. The token must include *Workers R2 Storage: Edit*. Set both keys only to use a separate R2 token. |
| `DOKPLOY_API_KEY` | optional override | Not needed. `up` creates the Dokploy admin account and an API key through Dokploy's API and keeps them in the encrypted state. Set it only to use a key you made by hand. |
| `HCLOUD_TOKEN` | compute `hetzner` | Read and write API token. Only when Hetzner creates the server. |
| `NETCUP_API_USERNAME`, `NETCUP_API_PASSWORD` | compute `netcup` | Required **only** when the firewall adapter is enabled (`config/providers/netcup.yaml` exists). Without it, apply the [rule table](netcup-firewall.md) by hand and no API credentials are needed. |
| `NETCUP_REFRESH_TOKEN` | compute `netcup` | Required **only** for `reinstall`, which wipes and reinstalls a Netcup server from CI. Create it with a one-time browser approval: [Netcup reinstall](netcup-reinstall.md#create-the-netcup-refresh-token). |
| `<app>.<NAME>` | apps with secrets | For example `shop.CLERK_SECRET_KEY`. |

Any key may also be supplied as an environment variable of the same name instead
of using Bitwarden (`secrets: env`).

**`secrets.source: ci` (your repository's own secrets).** Add each key above as a
**repository secret** of the infra repo (Settings, Secrets and variables, Actions)
and set `slots.secrets: ci`. GitHub only hands a workflow the secrets it names, so
the caller workflow must pass **`secrets: inherit`** (the generated one does). The
reusable workflow then forwards all of them to the engine as one masked JSON
environment variable, `SEALEDSTACK_CI_SECRETS` (`toJSON(secrets)`), which the
engine reads before any plain environment variable. The workflow does this
**only when `slots.secrets` is `ci`** (its first job reads that setting and outputs
`ci_secrets=true|false`); with Bitwarden, Doppler, Infisical or any other source the engine receives only
that source's credentials (`BWS_ACCESS_TOKEN`, `DOPPLER_TOKEN` and `DOPPLER_TOKEN_APPS`, or `INFISICAL_CLIENT_ID`, `INFISICAL_CLIENT_SECRET` and `INFISICAL_TOKEN`) and
`GH_TOKEN`, never the rest of your repository secrets. Two limits to know:

- Only **repository** secrets arrive. Environment secrets are not forwarded, and
  private repositories on GitHub Free have no environments at all.
- Anything the workflow can read is readable by every workflow in the repo that
  can run with those secrets; Bitwarden keeps the infrastructure credentials out
  of GitHub entirely, which is why it is the default.

## Cloudflare API token permissions

Create one token (My Profile, API Tokens, Create Token) with:

Account scope:

- Cloudflare Tunnel: Edit
- Access: Apps and Policies: Edit
- Workers R2 Storage: Edit

Zone scope (`example.com`):

- DNS: Edit
- Zone WAF: Edit
- Bot Management: Edit
- Single Redirect: Edit
- Transform Rules: Edit (response security headers, D-078)
- Zone Settings: Edit (minimum TLS version, D-079; custom ciphers only if you turn on `restrict_tls_ciphers`, which also needs the paid Advanced Certificate Manager)

`check` probes each permission and tells you which one is missing. The probes only read, so a token that can read a setting but not edit it still passes; the first `apply` is the real test. Your
**Account ID** and **Zone** go into `config/stack.yaml`; they are not secrets.

## Bot identity: fine-grained token

For now the bot identity (`sealedstack-bot`) is a **fine-grained personal access
token** ([D-058](maintainers/decisions.md)); a user-owned GitHub App comes later. Create it at
GitHub, Settings, Developer settings, Fine-grained tokens (the Setup checklist
links the form with the name, owner, expiry and permissions pre-filled):

- **Resource owner:** you (or the organisation that owns the repos).
- **Repository access:** *Only select repositories*: the infra repo and each
  onboarded app repo. Nothing else.
- **Expiration:** 90 days is a good start; put the date in your calendar.
- **Repository permissions** (everything else stays *No access*):

| Permission | Access | Why |
|---|---|---|
| Metadata | Read | Mandatory for every token; also used to look up a user's repository role (comment-command and approver checks). |
| Contents | Read and write | Read files and branches, and write the Setup, onboarding and version-bump branches. |
| Pull requests | Read and write | Open the Setup, onboarding and upgrade PRs; read PRs for the plan, approval and merge checks. |
| Issues | Read and write | The pinned Status issue, the living checklist and plan comments (PR comments go through the Issues API). |
| Variables | Read and write | Publish the deploy settings (compose ids, Dokploy URL, release trust ids, registry) to each app repo. |
| Deployments | Read | Read each app repo's latest successful `production` deployment, the released version recorded by the app release workflow (D-060), to redeploy it after a server rebuild. |
| Secrets | Read and write | Store the Dokploy API key as a repository secret in each app repo (the release job reads it; environment secrets are not available to private repos on GitHub Free). |
| Workflows | Read and write | PRs that add or change `.github/workflows/*` files (the app release workflow, version-bump PRs) are refused without it. |
| Administration (optional) | Read and write | Only to let sealedstack create the `production` environment itself. Skip it and create that environment by hand in each app repo. |

Add it as the **repository secret `GH_TOKEN`** in the infra repo. `check` probes
that the token can reach the infra repo and every app repo in `apps.yaml` with
harmless reads, names the permission that is missing, and warns about a classic
token or a missing `SEALEDSTACK_DOKPLOY_API_KEY` repository secret in an app repo. GitHub does not let a token read back
its own write levels, so write access is confirmed the first time it is used.

## Tailscale OAuth client

In Tailscale, Settings, OAuth clients, create a client with the scopes
`policy_file`, `auth_keys` and `devices:core`. Enable MagicDNS and HTTPS
certificates. Your existing tailnet policy is imported verbatim into
`config/tailscale-policy.hujson` by `check`, so you can review it before anything
is changed ([D-026](maintainers/decisions.md)).

## Bitwarden Secrets Manager layout

| Project | Holds |
|---|---|
| `infra` | The infrastructure keys in the tables above. |
| `prod` | `<app>.<NAME>` for production apps, for example `shop.CLERK_SECRET_KEY`. |
| `staging` | The same naming, for staging. |

The free plan allows three projects and three machine accounts. Give each machine
account the least access it needs: CI reads `infra` and `prod`, read-only.

## Using Doppler instead of Bitwarden

Set `slots.secrets: doppler` (or run Setup with `--secrets doppler`, [D-066](maintainers/decisions.md)).
sealedstack reads your secrets from **one Doppler config** over Doppler's API with a
**service token**; it never writes to Doppler, and no `doppler` program is needed.

**Create it once:**

1. In Doppler, create a project (for example `sealedstack`) and a config in it (for
   example `prd`).
2. Add every secret from the tables above to that config, using the names in
   [the naming rule](#doppler-secret-names) below. Everything those tables say goes
   "into Bitwarden" goes into this Doppler config instead.
3. Open the config, **Access**, **Generate**, name it `sealedstack-ci`, and leave
   **write access off**. Copy the token (it starts with `dp.st.` and is shown once).
4. In your infra repo add it as the GitHub repository secret **`DOPPLER_TOKEN`**. It
   is the only secret kept in GitHub (the Doppler equivalent of `BWS_ACCESS_TOKEN`).

**Why a service token:** Doppler scopes it to exactly one config and makes it
read-only by default, so the one secret CI holds can read that config and nothing
else. Personal, CLI and service-account tokens are refused on purpose, because they are
not scoped to one config.

**Optional: a second config for app secrets.** To keep app secrets (the `<app>.<NAME>`
keys) apart from the infra secrets, put them in another config of the same project,
generate a read-only service token for it, and add that as the repository secret
`DOPPLER_TOKEN_APPS`. When it is not set, app secrets are read from the same config as
the infra secrets.

### Doppler secret names

Doppler's default naming mode allows only upper-case letters, digits and underscores
(no dots) and no leading digit. sealedstack therefore looks a key up under the name as
written, then under the normalised name: upper-case, every other character replaced by
`_`.

| Key sealedstack asks for | Name to create in Doppler |
|---|---|
| `CLOUDFLARE_API_TOKEN`, `STATE_PASSPHRASE`, `SERVER_IP_SERVER_1` | the same |
| `shop.CLERK_SECRET_KEY` | `SHOP_CLERK_SECRET_KEY` |
| `my-app.DB_URL` | `MY_APP_DB_URL` |

If your workspace uses Doppler's permissive naming mode, the dotted name works as
written. A secret that exists but is empty is reported as missing.

**Limits to know:**

- One request per run fetches the whole config, so the Developer plan's limit of 120
  secret reads a minute is never close. sealedstack retries a rate-limit answer.
- Service-account tokens (which can span projects) are not supported yet.
- Not run against a live Doppler account yet, like every provider here
  ([limitations](limitations.md)).

## Using Infisical instead of Bitwarden

Set `slots.secrets: infisical` (or run Setup with `--secrets infisical`, [D-067](maintainers/decisions.md)).
sealedstack reads your secrets from **one folder of one Infisical project environment**
over Infisical's API with a **machine identity**; it never writes to Infisical, and no
`infisical` program is needed. Infisical Cloud (US and EU) and self-hosted instances work.

**Create it once:**

1. In Infisical, create a project and note its **project ID** (Project Settings).
2. Add every secret from the tables above to one environment (for example `prod`), using
   the names in [the naming rule](#infisical-secret-names) below. Everything those
   tables say goes "into Bitwarden" goes into this Infisical project instead.
3. Under **Access Control, Machine Identities**, create an identity with **Universal
   Auth**, add it to the project with a **read-only** role (it must be allowed to read
   secret values), and create a **client secret** for it. Copy the client ID and the
   client secret.
4. In your infra repo add them as the GitHub repository secrets
   **`INFISICAL_CLIENT_ID`** and **`INFISICAL_CLIENT_SECRET`**. They are the only secrets
   kept in GitHub (the Infisical equivalent of `BWS_ACCESS_TOKEN`).
5. Tell sealedstack where the secrets are. This is not secret, so it lives in
   `config/stack.yaml` (Setup writes the block with an empty `project_id`):

   ```yaml
   providers:
     secrets_infisical:
       project_id: 6a1b2c3d-0000-0000-0000-000000000000
       environment: prod       # the environment slug
       secret_path: /          # the folder with the infra secrets
   ```

| Setting | Meaning | Default |
|---|---|---|
| `project_id` | The project's ID (required). | none |
| `environment` | The environment slug. | `prod` |
| `secret_path` | The folder holding the infra secrets. | `/` |
| `apps_secret_path` | The folder holding the app secrets (the `<app>.<NAME>` keys). | `secret_path` |
| `site_url` | `https://eu.infisical.com` or your own host. Must be `https`. | `https://app.infisical.com` |

**Why Universal Auth:** the client ID and secret are exchanged for a short-lived access
token at the start of every run, so nothing in GitHub expires into a stale token. The
identity is limited to the roles you give it, so the secrets CI holds can read that
project and nothing else (read-only: sealedstack never writes). If you prefer a single
value, add a Token Auth access token as `INFISICAL_TOKEN` instead; it wins when both are
set, but it expires after its TTL (30 days by default) and must then be replaced.

**Optional: keep app secrets apart.** Put the app secrets in another folder (for
example `/apps`) and set `apps_secret_path: /apps`. To keep them out of the infra
identity's reach, restrict its role to the infra folder; a second identity for apps is
not supported yet.

### Infisical secret names

sealedstack looks a key up under the name as written, then under the normalised name:
upper-case, every other character replaced by `_`. Creating the normalised name always
works.

| Key sealedstack asks for | Name to create in Infisical |
|---|---|
| `CLOUDFLARE_API_TOKEN`, `STATE_PASSPHRASE`, `SERVER_IP_SERVER_1` | the same |
| `shop.CLERK_SECRET_KEY` | `SHOP_CLERK_SECRET_KEY` |
| `my-app.DB_URL` | `MY_APP_DB_URL` |

A secret that exists but is empty is reported as missing. A secret whose value is hidden
from the identity is reported as an error that names the missing "read value" permission.

**Limits to know:**

- One request per run fetches the whole folder (plus one login), so rate limits are never
  close. sealedstack retries a rate-limit answer.
- Secrets in subfolders and secret imports are not read; keep sealedstack's secrets in
  the one folder.
- Not run against a live Infisical account yet, like every provider here
  ([limitations](limitations.md)). The first live run confirms the v4 list call and the
  secret-name rules.

## Server credentials

- **Existing machine (Netcup, any SSH host).** The first bootstrap uses the root
  password and the public IP from the secret source, once, over TCP 22 (open it at
  the provider firewall for that window). Bootstrap then disables password and root
  login, so the password is useless afterwards. See [Netcup firewall](netcup-firewall.md).
- **Provider with an API (Hetzner).** Only the provider token is needed. The server
  and its firewall are created for you, and the IP is learned and kept in encrypted
  state.

## `config/` files

Setup creates these in your infra repo. You edit them through pull requests.

- `config/stack.yaml`: slots, Cloudflare account/zone/team, gate policies, edge
  security, state bucket, `secrets`, `admin.ssh_keys` and `approvers`.
- `config/servers.yaml`: server name (also its Tailscale hostname), provider,
  role (`control` or `worker`) and OS, plus `bootstrap` key names for existing
  machines.
- `config/apps.yaml`: per-app source, servers, hostnames, access.
- `config/tailscale-policy.hujson`: your tailnet policy, imported by `check`.

`check` validates all of them against the schemas before anything changes.

## Private app repositories and the drift job

- **Private app repos:** apps deploy as prebuilt images from a registry, so the
  server never needs repository access ([D-024](maintainers/decisions.md)). The infra repo
  publishes the deploy settings (compose ID, CI identity) to each app repo
  ([D-023](maintainers/decisions.md)).
- **Drift job:** scheduled drift is off unless the repository variable
  `SEALEDSTACK_DRIFT_ENABLED` is `true` (D-059). When on, it reads the same secrets as `check`. Use
  a **separate** machine account for it if you want to keep app CI's access to
  `prod` only.
