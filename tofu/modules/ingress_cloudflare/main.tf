# ingress_cloudflare (spec section 8, D-013): one tunnel per app, named ss-<app>,
# with DNS and an optional Access check enforced at the connector.

resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.account_id
  name       = "ss-${var.app}"
  config_src = "cloudflare"
}

locals {
  # Every entry has the same shape (hostname, path, service, origin_request). `origin_request` is null unless the gate is
  # cloudflare_access and supplied an audience tag for this hostname; a non-cloudflare gate (authentik, oauth2-proxy,
  # ...) has no aud tag, so the lookup is guarded. The audience tags only exist after the Access apps are created, so
  # the condition is unknown at plan time: choosing between "an object" and "an object with other attributes" made the
  # whole entry unknown and the provider then reported `service` as missing (first live `up`). Object-or-null keeps
  # `hostname` and `service` known.
  #
  # A gated hostname's rule makes cloudflared demand an Access JWT on every request. A request to a
  # `user_auth.webhook_paths` path passes Access through its Bypass app and carries no JWT, so without a rule of its own
  # cloudflared refused it with a bare 403 (paisa, 0.6.39; D-090). The path rule comes first (cloudflared takes the first
  # match) and has no access check. Like the Access app paths, each declared path covers itself and everything under it.
  webhook_paths = [for path in var.webhook_paths : trimsuffix(path, "/")]
  webhook_regex = format("^(%s)(/.*)?$", join("|", [
    for path in local.webhook_paths : replace(path, "/([.+*?()\\[\\]{}|^$\\\\])/", "\\$1")
  ]))
  webhook_rules = [
    for hostname in var.hostnames : {
      hostname       = hostname
      path           = local.webhook_regex
      service        = "http://dokploy-traefik:80"
      origin_request = null
    } if var.gated && length(local.webhook_paths) > 0
  ]

  ingress = concat(
    local.webhook_rules,
    [
      for hostname in var.hostnames : {
        hostname = hostname
        path     = null
        service  = "http://dokploy-traefik:80"
        origin_request = var.gated && var.team_name != "" && lookup(var.aud_by_hostname, hostname, "") != "" ? {
          access = {
            required  = true
            team_name = var.team_name
            aud_tag   = [var.aud_by_hostname[hostname]]
          }
        } : null
      }
    ],
    [
      # Catch-all: anything not matched above is refused.
      {
        hostname       = null
        path           = null
        service        = "http_status:404"
        origin_request = null
      }
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
