# Getting started

This is the full first-time setup: what accounts and credentials you need, where
to get them, and the commands to go from an empty box to a running app.

## The stack

sealedstack is assembled from **slots** — one implementation per external
dependency — so "the stack" is simply your choice of provider in each slot. Pick
it first. This guide sets up the **reference stack** below:

| Slot | Reference provider | Other options (`just providers <slot>`) |
|---|---|---|
| compute | hetzner | digitalocean, vultr, linode, scaleway, aws, gcp, azure, netcup, any-ssh |
| mesh | tailscale | zerotier, netbird, headscale, … |
| ingress | cloudflare | ngrok, caddy, traefik, nginx, frp, pangolin |
| gate | cloudflare_access | authentik, authelia, oauth2-proxy, pomerium, keycloak, … |
| secrets | bitwarden | sops, aws_ssm, gcp, azure_kv, openbao, vault |
| backup | r2 | s3, b2, wasabi, spaces, minio, storj, … |
| platform | dokploy | (more planned) |
| user_auth | clerk | oidc (any provider) |
| ci | github-actions | gitlab-ci, gitea-actions, woodpecker, … |

**Any slot can be swapped** with one line in `config/stack.yaml`, and compute is
chosen per server in `config/servers.yaml`. The sections below configure the
reference stack; if you pick a different provider in a slot, use **its**
credentials instead of the reference one (for example `DIGITALOCEAN_TOKEN`
instead of `HCLOUD_TOKEN`, or `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` for a
non-R2 backup). Cloudflare is used by the reference stack for DNS, R2 state and
WAF; it is still needed unless you also change `slots.ingress`, `slots.gate` and
`state.backend`. See [providers.md](providers.md).

## 0. What you need

| Thing | Why | Where |
|---|---|---|
| Domain on Cloudflare nameservers | tunnels + Access need a Cloudflare zone | dash.cloudflare.com |
| Cloudflare account (Free plan) | Tunnel, Access, WAF, R2 | dash.cloudflare.com |
| Tailscale account (MagicDNS on) | admin access over a private mesh | tailscale.com |
| Bitwarden account + free org with Secrets Manager | machine secrets | bitwarden.com |
| GitHub account + `gh` | app repos; `gh` reads `sealedstack.yaml` | github.com |
| A server (or a provider token to create one) | the origin | see provider table |
| Docker + `just` on your laptop | everything runs in the toolbox | `brew install just` |
| Clerk account | only for `user_auth: clerk` apps | clerk.com |

## 1. Cloudflare

1. Add your domain to Cloudflare and point the registrar at Cloudflare's
   nameservers.
2. Enable **Zero Trust** (pick a team name, e.g. `example`). The Access login
   will be `example.cloudflareaccess.com`.
3. Create an **API token** (My Profile → API Tokens → Create):
   - Account: **Cloudflare Tunnel: Edit**, **Access: Apps and Policies: Edit**,
     **Workers R2 Storage: Edit**
   - Zone (`example.com`): **DNS: Edit**, **Zone WAF: Edit**,
     **Bot Management: Edit**, **Single Redirect: Edit**
4. Note your **Account ID** and **Zone ID** (not secrets).

## 2. Tailscale

1. Sign in; enable **MagicDNS** and **HTTPS certificates**.
2. Create an **OAuth client** (Settings → OAuth clients) with scopes
   `policy_file`, `auth_keys` and `devices:core`. This is
   `TAILSCALE_OAUTH_CLIENT_ID` / `TAILSCALE_OAUTH_CLIENT_SECRET`; server auth
   keys are minted from it (Task 4.2), so no long-lived API key is needed.

## 3. Bitwarden Secrets Manager

1. Create a free organisation and enable Secrets Manager.
2. Create three projects: `infra`, `prod`, `staging`.
3. Create one **machine account** for the laptop and copy its access token — this
   is `BWS_ACCESS_TOKEN` (the only secret you handle directly).
4. Add the `infra` secrets (see [providers](#5-provider-credentials) and
   [prerequisites](prerequisites.md)):

   | Secret | Value |
   |---|---|
   | `CLOUDFLARE_API_TOKEN` | from step 1 |
   | `TAILSCALE_OAUTH_CLIENT_ID` / `TAILSCALE_OAUTH_CLIENT_SECRET` | from step 2 |
   | `state_passphrase` | any long random string |
   | `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | created in step 4 |
   | `admin_sudo_password` | a password for the Linux `admin_user` |
   | `DOKPLOY_API_KEY` | created by Dokploy after first boot |
   | provider tokens | `HCLOUD_TOKEN`, `DIGITALOCEAN_TOKEN`, ... |

5. **R2 credentials** (for state + backups): Cloudflare dashboard → R2 → **Manage
   R2 API Tokens** → Create → give it Object Read & Write. Copy the access key
   id and secret key into `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`.

## 4. GitHub

Authenticate the toolbox's `gh` by exporting a token so `just render` can read
app manifests:

```sh
export GH_TOKEN=$(gh auth token)   # or create a PAT with repo:read
```

## 5. Provider credentials

Pick where the server runs. For providers that **create** the server, add the
provider token to `infra`. For providers where you **bring your own** box, you
need the box's root password on first boot.

| Provider | Creates server? | Credential to add | Notes |
|---|---|---|---|
| hetzner | yes | `HCLOUD_TOKEN` | API token, read/write |
| digitalocean | yes | `DIGITALOCEAN_TOKEN` | API token |
| vultr | yes | `VULTR_API_KEY` | API key |
| hostinger | yes | `HOSTINGER_TOKEN` | hPanel → API |
| netcup | no | `NETCUP_API_USERNAME` / `NETCUP_API_PASSWORD` | order the VPS, note `external_id`; firewall via SCP API ([config](providers.md)) |
| any-ssh | no | — | you provide the IP and apply the firewall |

First boot also needs `ROOT_PASSWORD` (provider root password) and your
your public key in `config/stack.yaml` (`admin.ssh_keys`).

## 6. Write your config

```sh
just init                     # interactive wizard → writes config/
# or copy and edit:
cp config/stack.example.yaml config/stack.yaml
cp config/servers.example.yaml config/servers.yaml
cp config/apps.example.yaml config/apps.yaml
```

Set your `slots:` (the stack above), the `admin_user` and domain, and each
server's `provider`. Anything you leave out uses the reference default.

## 7. First run

```sh
export BWS_ACCESS_TOKEN=...
export GH_TOKEN=...
# add your public key(s) to config/stack.yaml under admin.ssh_keys
export ROOT_PASSWORD=...            # omit to be prompted

just validate                       # schema + registry + compose lint
just doctor                         # confirms secrets/tools for your providers
just state-init                     # create the encrypted state bucket (once)
just server-up web-1                # create + bootstrap (existing boxes read SERVER_IP_<NAME>)
just apply                          # edge + platform
```

## 8. Verify

```sh
curl -sI https://hello.example.com | head -1     # 302 → Access login
ssh web-1                                         # over Tailscale
nc -zv <public-ip> 22 80 443 3000                 # all must time out
just server-sync && just apply                    # no changes
```

## Troubleshooting

- **`just doctor` lists missing secrets** — add them to Bitwarden `infra` or
  export them as env vars of the same name.
- **`just render` warns "manifest unresolved"** — set `GH_TOKEN`; harmless for
  image-only apps.
- **Server unreachable over Tailscale** — check the node joined with the right
  hostname (`tailscale status`), and that MagicDNS is on.
