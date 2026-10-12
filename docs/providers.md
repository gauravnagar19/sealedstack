# Providers

sealedstack is built around **slots** (spec section 4). Every external
dependency is a slot with a catalog of implementations, so you can swap
providers with a one-line config change. The catalog is the **registry**.

```sh
just providers              # every slot
just providers backup       # one slot
```

The registry files are `registry/<slot>.yaml`, validated against
`schemas/registry.schema.json`. Each entry has:

- `status`: `supported` (passed the live acceptance suite) ·
  `experimental` (code exists, not live-tested) · `planned` (no implementation)
- `secrets`: the secret names it needs (checked by `just doctor`)
- `config`: the per-server/per-app config keys it reads
- `impl`: where the code lives (`tofu_module`, `ansible_role`, `adapter`, `stack`)
- `creates_server` (compute only): `just server-up` will provision it and
  discover the IP

`just validate` fails if you select an implementation that is missing or still
`planned` — you never get a silent wrong deploy.

## Status by slot

Everything is `experimental` until it passes the live acceptance suite (D-027); see status (private). None are `supported` yet; `planned` entries have no implementation.

| Slot | Experimental | Planned / out of scope |
|---|---|---|
| compute | any-ssh, hetzner, hostinger, digitalocean, vultr, linode, scaleway, aws, gcp, azure, oracle, upcloud, ovh, civo (native); netcup, contabo (adapters — fill `config/providers/<id>.yaml`) | — |
| mesh | tailscale, zerotier (native); headscale, netbird, nebula, netmaker, innernet, firezone, wireguard, tailscale-ssh (Ansible) | — |
| ingress | cloudflare, ngrok (native); caddy, traefik, nginx, frp, pangolin, tailscale-funnel, zrok, bore (Ansible) | — |
| gate | cloudflare_access (native); authentik, authelia, oauth2-proxy, pomerium, keycloak, zitadel, logto (Ansible); okta, auth0 (OIDC adapter) | — |
| secrets | bitwarden, aws_ssm, gcp, azure_kv, openbao, vault (native); sops, infisical, doppler, pass, onepassword (adapters) | — |
| backup | r2 + 14 S3-compatible stores, gcs, azure_blob (native); hetzner_storage_box (Ansible) | — |
| platform | dokploy (native); coolify, caprover, portainer (Ansible) | kamal, kubernetes, nomad, fly, render, railway (out of scope — spec §1) |
| user_auth | clerk, oidc, better_auth, zitadel, logto, authentik, keycloak, supabase, firebase, workos, auth0 (config/adapter) | — |
| ci | github-actions, gitlab-ci, gitea-actions, drone, jenkins, buildkite, circleci, bamboo, woodpecker, aws-codebuild (config-only) | — |

CI/CD is the `ci` slot (`registry/ci.yaml`): the app repo's own release
pipeline, GitHub Actions by default. See [ci-cd.md](ci-cd.md).

## Choose a provider

Set it in `config/stack.yaml`:

```yaml
slots:
  backup: wasabi      # was r2
  secrets: aws_ssm    # was bitwarden
  mesh: tailscale
```

For **compute**, providers are chosen per server in `config/servers.yaml`:

```yaml
servers:
  web-1:
    provider: digitalocean
    role: control
    os: ubuntu-24.04
    server_type: s-1vcpu-2gb
    location: fra1
```

## Add a provider

The framework is registry-driven, so most of the work is one entry plus code
for the provider itself.

1. **Register it** in `registry/<slot>.yaml`:

   ```yaml
   - id: coolify
     name: Coolify
     status: planned            # flip to native/adapter when implemented
     creates_server: false
     summary: Self-hosted PaaS.
     secrets: [COOLIFY_API_TOKEN]
     config: [endpoint]
     impl: { tofu_module: platform_coolify, stack: platform }
     docs: https://coolify.io/
   ```

2. **Implement it** using the pattern that fits:

   | Pattern | When | Look at |
   |---|---|---|
   | Native OpenTofu module | an OpenTofu provider exists | `tofu/modules/compute_hetzner`, `backup_s3`, `secrets_aws_ssm` |
   | REST adapter | an API exists but no provider | `tofu/modules/compute_netcup` + `engine/sealedstack/firewall.py` |
   | Ansible role | a self-hosted service on the host | `ansible/roles/mesh_tailscale` |
   | CLI adapter | a CLI tool (sops, pass, 1password) | `engine/sealedstack/sopssecrets.py` |

   Wire it into the stack that composes the slot (`tofu/stacks/edge`,
   `tofu/stacks/compute`, `tofu/stacks/platform`).

3. **Add a sample** under `config/providers/<slot>_<impl>.example.yaml` if it
   needs non-secret config, and list the secrets in the registry entry.

4. Run `just validate` and `just doctor`.

### Compute providers

`just server-up <name>` provisions the server when the registry entry has
`creates_server: true` (hetzner, hostinger, digitalocean, vultr, linode,
scaleway, aws, gcp, azure, oracle, upcloud, ovh, civo) and discovers the IP.
Otherwise (netcup, contabo, any-ssh) you pass the IP. Firewall handling is
declared per provider as `firewall: native | api | manual | none`:

- **native** — an OpenTofu firewall resource (hetzner, digitalocean, vultr,
  linode, scaleway, aws, gcp, azure, oracle, upcloud, ovh, civo). Each default
  rule table opens **TCP 22** temporarily so `server-up` can SSH in before the
  mesh is up; the host firewall (UFW + DOCKER-USER) closes 22 on the WAN
  interface after `site.yml`.
- **manual** — `just server-up` prints the rule table and waits for you
  ([netcup-firewall.md](netcup-firewall.md)); this is netcup, contabo and
  any-ssh today.
- **api** — the REST adapter applies the rule table automatically once
  `config/providers/<id>.yaml` exists (the netcup/contabo modules).
- **none** — no provider firewall; the host UFW/DOCKER-USER protects the origin
  (hostinger).

### The REST adapter

For providers with a firewall API but no OpenTofu provider, copy
`config/providers/netcup.example.yaml` to `config/providers/<provider>.yaml`
and fill it from that provider's API docs:

```yaml
base_url: https://www.servercontrolpanel.de/api/v1
auth: { type: basic }        # basic (API_USERNAME/API_PASSWORD) or bearer (API_TOKEN)
firewall:
  method: PUT
  path: /servers/{server_id}/firewall
  body: |
    { "rules": {{rules}} }
```

`{server_id}` is replaced with the server's `external_id`; `{{rules}}` with the
rule array from `tofu/stacks/compute`. While the file is absent the provider is
a no-op and the rule table is applied by hand.

> Netcup endpoints above are placeholders — confirm them against the SCP API
> documentation before enabling.

## Configuration surface

- Per-server sizing: `server_type`, `location`, `image`, `os_id`, `plan`,
  `template_id`, `data_center_id`, `external_id` in `config/servers.yaml`.
- Backup destination: a `backup:` block in `config/stack.yaml`.
- Anything else a provider needs goes under `providers:` in `config/stack.yaml`,
  keyed by `<slot>_<impl>` (e.g. `gate_authentik: { server_url: ... }`).
