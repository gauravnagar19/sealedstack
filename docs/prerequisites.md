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
| Bitwarden account with a free organisation and Secrets Manager | yes | The default secret source. sealedstack only reads from it. |
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
| Bitwarden machine-account token | GitHub repository secret `BWS_ACCESS_TOKEN` | The only secret in GitHub. Give the machine account **read-only** access to the secrets project (default `infra`). See [limitations](limitations.md#approvals-on-github-free-private-repositories-what-they-do-and-dont-protect). |
| Cloudflare API token | Bitwarden: `CLOUDFLARE_API_TOKEN` | [Permissions below](#cloudflare-api-token-permissions). |
| Tailscale OAuth client | Bitwarden: `TAILSCALE_OAUTH_CLIENT_ID`, `TAILSCALE_OAUTH_CLIENT_SECRET` | Scopes `policy_file`, `auth_keys`, `devices:core`. Server auth keys are minted from it, so no long-lived API key is needed. |
| State passphrase | Bitwarden: `STATE_PASSPHRASE` | At least 16 random characters. Encrypts the OpenTofu state. |
| Existing server: public IP and root password | Bitwarden: `SERVER_IP_<NAME>`, `ROOT_PASSWORD_<NAME>` | `<NAME>` is the server name upper-cased with `-` as `_`, so `server-1` gives `SERVER_IP_SERVER_1`. Used once by the first bootstrap; delete them afterwards. |
| Admin SSH public key(s) | `config/stack.yaml`: `admin.ssh_keys` | A public key, not a secret. Installed on every server at bootstrap. |
| Bot identity | GitHub repository secret `GH_TOKEN` | A **fine-grained personal access token** limited to the infra repo and the app repos ([permissions below](#bot-identity-fine-grained-token)). Its pull requests trigger the plan workflows, which the built-in `GITHUB_TOKEN` cannot ([D-058](decisions.md)). |
| Approvers | `config/stack.yaml`: `approvers` | GitHub usernames allowed to approve write commands. Setup defaults it to the repository owner. |

### Key names are declared in config

Setup writes the **name of every secret** into your config, so the mapping from
setting to Bitwarden entry is visible and reviewable ([D-050](decisions.md)):

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

```yaml
# config/servers.yaml (an existing machine)
servers:
  server-1:
    provider: netcup
    role: control
    os: ubuntu-24.04
    bootstrap:
      method: password
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
(see the [user experience](user-experience.md) and [decisions](decisions.md)
D-044).

| Key | When | Notes |
|---|---|---|
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | optional override | Not needed. The R2 S3 keys are derived from the Cloudflare token (D-054): key id = the token's id, secret = the SHA-256 hex of the token value. The token must include *Workers R2 Storage: Edit*. Set both keys only to use a separate R2 token. |
| `DOKPLOY_API_KEY` | optional override | Not needed. `up` creates the Dokploy admin account and an API key through Dokploy's API and keeps them in the encrypted state. Set it only to use a key you made by hand. |
| `HCLOUD_TOKEN` | compute `hetzner` | Read and write API token. Only when Hetzner creates the server. |
| `NETCUP_API_USERNAME`, `NETCUP_API_PASSWORD` | compute `netcup` | Required **only** when the firewall adapter is enabled (`config/providers/netcup.yaml` exists). Without it, apply the [rule table](netcup-firewall.md) by hand and no API credentials are needed. |
| `<app>.<NAME>` | apps with secrets | For example `shop.CLERK_SECRET_KEY`. |

Any key may also be supplied as an environment variable of the same name instead
of using Bitwarden (`secrets: env` or `ci`).

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

`check` probes each permission and tells you exactly which one is missing. Your
**Account ID** and **Zone** go into `config/stack.yaml`; they are not secrets.

## Bot identity: fine-grained token

For now the bot identity (`sealedstack-bot`) is a **fine-grained personal access
token** ([D-058](decisions.md)); a user-owned GitHub App comes later. Create it at
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
is changed ([D-026](decisions.md)).

## Bitwarden Secrets Manager layout

| Project | Holds |
|---|---|
| `infra` | The infrastructure keys in the tables above. |
| `prod` | `<app>.<NAME>` for production apps, for example `shop.CLERK_SECRET_KEY`. |
| `staging` | The same naming, for staging. |

The free plan allows three projects and three machine accounts. Give each machine
account the least access it needs: CI reads `infra` and `prod`, read-only.

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
  server never needs repository access ([D-024](decisions.md)). The infra repo
  publishes the deploy settings (compose ID, CI identity) to each app repo
  ([D-023](decisions.md)).
- **Drift job:** scheduled drift is off unless the repository variable
  `SEALEDSTACK_DRIFT_ENABLED` is `true` (D-059). When on, it reads the same secrets as `check`. Use
  a **separate** machine account for it if you want to keep app CI's access to
  `prod` only.
