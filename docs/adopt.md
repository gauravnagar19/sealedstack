# Adopting existing resources

If you already have Cloudflare DNS records, WAF rules, an Access app, a tunnel,
or a Tailscale ACL — from a previous guide or manual setup — the first
`just apply` would try to **create** duplicates or **overwrite** them. Adopt them
first so OpenTofu takes ownership instead.

> This is why the Tailscale policy is now generated from `config/stack.yaml`
> `the policy file (imported at init)` (so your other devices keep working, C5) and why public
> app hostnames get an explicit Access bypass (H4).

## Cloudflare

Cloudflare allows one custom-ruleset per zone per phase, so a manually created
`block-scanner-paths` rule will clash. Import instead of recreating:

```sh
tofu -chdir=tofu/stacks/edge import \
  cloudflare_ruleset.waf[0] <zone_id>/<ruleset_id>       # custom firewall phase (count-indexed)
```

Do the same for the dynamic-redirect ruleset (`cloudflare_ruleset.redirects[0]`),
each `cloudflare_dns_record["<hostname>"]`, each
`cloudflare_zero_trust_access_application` (count-indexed `[0]` or keyed
`["<hostname>"]`), the `cloudflare_zero_trust_tunnel_cloudflared["<app>"]`, and the
R2 bucket if it already exists. Find ids with the Cloudflare API or
`tofu -chdir=tofu/stacks/edge plan` (it lists what it wants to create).

Practical path:

1. `just render --allow-unresolved` (or with GitHub access), then
   `tofu -chdir=tofu/stacks/edge plan` and read the "will be created" list.
2. For each resource that already exists, `tofu import` it.
3. Re-plan until the only changes are the ones you intend.

## Tailscale

The `tailscale_acl` resource overwrites the **whole** policy file (D-014). To
keep the rest of your tailnet working:

1. Export your current policy (Tailscale admin → Access controls → copy), or
   `tofu -chdir=tofu/stacks/edge import 'module.mesh_tailscale[0].tailscale_acl.this' acl`.
2. Put your existing rules under `the policy file (imported at init)` (and `mesh.extra_ssh`) in
   `config/stack.yaml`. They are merged with the sealedstack base grants.
3. Add `mesh.tests` (e.g. admin can reach `tag:server:22`, `tag:ci` cannot).
4. Plan and review the diff before applying.

Alternatively, use a **dedicated tailnet** for servers.

## Resources sealedstack refuses to destroy

Databases (`dokploy_postgres`, `dokploy_redis`) and every backup bucket carry
`lifecycle { prevent_destroy = true }` (C2). If a plan wants to destroy one,
that is a signal something is wrong — investigate before overriding.
