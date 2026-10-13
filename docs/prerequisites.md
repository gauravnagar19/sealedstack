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
| State passphrase | Bitwarden: `state_passphrase` | At least 16 random characters. Encrypts the OpenTofu state. |
| Existing server: public IP and root password | Bitwarden: `SERVER_IP_<NAME>`, `ROOT_PASSWORD_<NAME>` | `<NAME>` is the server name upper-cased with `-` as `_`, so `server-1` gives `SERVER_IP_SERVER_1`. Used once by the first bootstrap; delete them afterwards. |
| Admin SSH public key(s) | `config/stack.yaml`: `admin.ssh_keys` | A public key, not a secret. Installed on every server at bootstrap. |
| Bot identity | Bitwarden: `SEALEDSTACK_BOT_APP_ID`, `SEALEDSTACK_BOT_PRIVATE_KEY`, `SEALEDSTACK_BOT_INSTALLATION_ID` | A GitHub App (`sealedstack-bot`) created from the checklist's one-click manifest link. Its pull requests trigger the plan workflows, which the built-in `GITHUB_TOKEN` cannot. |
| Approvers | `config/stack.yaml`: `approvers` | GitHub usernames allowed to approve write commands. Setup defaults it to the repository owner. |

### Key names are declared in config

Setup writes the **name of every secret** into your config, so the mapping from
setting to Bitwarden entry is visible and reviewable ([D-050](decisions.md)):

```yaml
# config/stack.yaml
secrets:
  project: infra            # the Bitwarden Secrets Manager project
  keys:                     # logical name -> key in the secret source
    CLOUDFLARE_API_TOKEN: CLOUDFLARE_API_TOKEN
    state_passphrase: STATE_PASSPHRASE   # example of a renamed key
```

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

An entry you leave out defaults to the logical name. Error messages and the
checklist always show both names, for example
`state_passphrase (source key STATE_PASSPHRASE)`. App secrets default to
`<app>.<NAME>` in the app-secrets project (`prod`) and can be remapped per app in
`config/apps.yaml` under `secrets:`.

## Other secrets the current implementation reads

These are read from the same Bitwarden project today. They are listed so nothing
surprises you at `up` time. Some are planned to be generated or derived instead
(see the [user experience](user-experience.md) and [decisions](decisions.md)
D-044).

| Key | When | Notes |
|---|---|---|
| `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` | optional override | Not needed. The R2 S3 keys are derived from the Cloudflare token (D-054): key id = the token's id, secret = the SHA-256 hex of the token value. The token must include *Workers R2 Storage: Edit*. Set both keys only to use a separate R2 token. |
| `admin_sudo_password` | server bootstrap and sync | A password for the Linux admin user on the server. |
| `DOKPLOY_API_KEY` | platform stage | Created in the Dokploy panel after the first bootstrap (see the [limitation in getting started](getting-started.md#5-merge-pr-1-then-run-up)). |
| `DOKPLOY_SSH_PRIVATE_KEY`, `DOKPLOY_SSH_PUBLIC_KEY` | worker servers | Generated on the first run and stored in the secret source. |
| `HCLOUD_TOKEN` | compute `hetzner` | Read and write API token. Only when Hetzner creates the server. |
| `NETCUP_API_USERNAME`, `NETCUP_API_PASSWORD` | compute `netcup` | Only needed for the firewall adapter; otherwise apply the [rule table](netcup-firewall.md) by hand. |
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
- **Drift job:** the scheduled drift check reads the same secrets as `check`. Use
  a **separate** machine account for it if you want to keep app CI's access to
  `prod` only.
