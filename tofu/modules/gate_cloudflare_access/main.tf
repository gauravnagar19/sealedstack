# gate_cloudflare_access (spec section 8): reusable policies, a self-hosted app
# per gated hostname, a deny-by-default catch-all, and path bypasses for
# user_auth webhooks.

resource "cloudflare_zero_trust_access_policy" "policy" {
  for_each = var.policies

  account_id       = var.account_id
  name             = each.key
  decision         = "allow"
  session_duration = each.value.session
  include          = [for email in each.value.emails : { email = { email = email } }]
}

resource "cloudflare_zero_trust_access_application" "gated" {
  for_each = var.gated

  account_id       = var.account_id
  zone_id          = var.zone_id
  name             = "sealedstack: ${each.key}"
  type             = "self_hosted"
  domain           = each.key
  session_duration = var.policies[each.value].session
  policies         = [{ id = cloudflare_zero_trust_access_policy.policy[each.value].id }]
}

resource "cloudflare_zero_trust_access_policy" "deny_all" {
  count = var.catch_all ? 1 : 0

  account_id = var.account_id
  name       = "sealedstack-catch-all-deny"
  decision   = "deny"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "catch_all" {
  count = var.catch_all ? 1 : 0

  account_id = var.account_id
  zone_id    = var.zone_id
  name       = "sealedstack: catch-all"
  type       = "self_hosted"
  domain     = "*.${var.domain}"
  policies   = [{ id = cloudflare_zero_trust_access_policy.deny_all[0].id }]
}

# Public hostnames under *.<domain> would otherwise be denied by the catch-all
# (H4). Give each an explicit bypass so `access: public` apps still work.
resource "cloudflare_zero_trust_access_policy" "public_bypass" {
  count = length(var.public_hostnames) > 0 ? 1 : 0

  account_id = var.account_id
  name       = "sealedstack-public-bypass"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "public_bypass" {
  for_each = toset(var.public_hostnames)

  account_id = var.account_id
  zone_id    = var.zone_id
  name       = "sealedstack: ${each.value} (public)"
  type       = "self_hosted"
  domain     = each.value
  policies   = [{ id = cloudflare_zero_trust_access_policy.public_bypass[0].id }]
}

locals {
  bypass_pairs = {
    for pair in flatten([for host, paths in var.bypass : [for path in paths : { host = host, path = path }]]) :
    "${pair.host}${pair.path}" => pair
  }
}

resource "cloudflare_zero_trust_access_policy" "bypass" {
  for_each = local.bypass_pairs

  account_id = var.account_id
  name       = "sealedstack-bypass-${replace(each.key, "/[^a-z0-9]+/", "-")}"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "bypass" {
  for_each = local.bypass_pairs

  account_id = var.account_id
  zone_id    = var.zone_id
  name       = "sealedstack: bypass ${each.key}"
  type       = "self_hosted"
  domain     = "${each.value.host}${each.value.path}"
  policies   = [{ id = cloudflare_zero_trust_access_policy.bypass[each.key].id }]
}
