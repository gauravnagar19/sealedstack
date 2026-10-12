# ingress_ngrok (spec section 8, D-013): a reserved domain and a public cloud
# endpoint per hostname, forwarding to an internal agent endpoint. The connector
# runs an ngrok agent with the credential token and forwards to the upstream.
#
# ngrok's cloud endpoints must terminate via a terminating action, so an
# optional OAuth action is followed by a `forward-internal` to the internal
# endpoint the connector binds.

locals {
  internal_url = var.internal_url != "" ? var.internal_url : "http://ss-${var.app}.internal:80"

  gating = var.gated ? [
    {
      actions = [
        {
          type = "oauth"
          config = {
            provider = var.oauth_provider
          }
        }
      ]
    }
  ] : []

  forward = [
    {
      actions = [
        {
          type = "forward-internal"
          config = {
            url = local.internal_url
          }
        }
      ]
    }
  ]

  traffic_policy = jsonencode({
    on_http_request = concat(local.gating, local.forward)
  })
}

resource "ngrok_domain" "this" {
  for_each = toset(var.hostnames)

  domain      = each.value
  description = "sealedstack: ${var.app}"
  metadata    = jsonencode({ app = var.app, team_name = var.team_name })
}

resource "ngrok_cloud_endpoint" "this" {
  for_each = toset(var.hostnames)

  url         = "https://${ngrok_domain.this[each.key].domain}"
  description = "sealedstack: ${var.app}"
  metadata    = jsonencode({ app = var.app, team_name = var.team_name })

  traffic_policy = local.traffic_policy
}

resource "ngrok_credential" "this" {
  description = "sealedstack connector for ${var.app} (ss-${var.app})"
  metadata    = jsonencode({ app = var.app })
  acl         = [for hostname in var.hostnames : "bind:${hostname}"]
}
