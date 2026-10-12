# ingress_cloudflare (spec section 8, D-013): one tunnel per app, named ss-<app>,
# with DNS and an optional Access check enforced at the connector.

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.account_id
  name       = "ss-${var.app}"
  config_src = "cloudflare"
}

locals {
  ingress = concat(
    [
      for hostname in var.hostnames : merge(
        {
          hostname = hostname
          service  = "http://dokploy-traefik:80"
        },
        # Only attach originRequest.access when the gate is cloudflare_access and
        # supplied an audience tag for this hostname. A non-cloudflare gate
        # (authentik, oauth2-proxy, ...) has no aud tag; `gated` is still true,
        # so guard the lookup instead of indexing a missing key.
        var.gated && var.team_name != "" && lookup(var.aud_by_hostname, hostname, "") != "" ? {
          origin_request = {
            access = {
              required  = true
              team_name = var.team_name
              aud_tag   = [var.aud_by_hostname[hostname]]
            }
          }
        } : {},
      )
    ],
    [
      # Catch-all: anything not matched above is refused.
      { service = "http_status:404" }
    ],
  )
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
  config     = { ingress = local.ingress }
}

resource "cloudflare_dns_record" "this" {
  for_each = toset(var.hostnames)

  zone_id = var.zone_id
  name    = each.value
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "sealedstack: ${var.app}"
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
}
