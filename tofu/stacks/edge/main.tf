# edge stack (spec section 8): composes mesh, ingress, gate, backup and secrets.
# The backup and secrets implementations are chosen by config/stack.yaml slots,
# so any registered provider can be plugged in.

# Provider instances are declared as *aliased* configurations with `for_each`.
# An aliased provider configuration with an empty `for_each` has zero instances
# and is therefore never configured, so an unselected slot never touches its
# provider (no inert placeholder values). Every resource and module selects the
# single active instance explicitly as `<provider>.<alias>["default"]`.
locals {
  on  = { default = true }
  off = {}
}

provider "cloudflare" {
  alias    = "this"
  for_each = local.cloudflare_needed ? local.on : local.off

  api_token = var.cloudflare_api_token
}

provider "tailscale" {
  alias    = "this"
  for_each = local.tailscale_needed ? local.on : local.off

  oauth_client_id     = var.tailscale_oauth_client_id
  oauth_client_secret = var.tailscale_oauth_client_secret
}

provider "aws" {
  alias    = "backup"
  for_each = local.aws_backup_needed ? local.on : local.off

  region     = var.backup_region != "" ? var.backup_region : "us-east-1"
  access_key = var.backup_access_key_id
  secret_key = var.backup_secret_access_key

  endpoints {
    s3 = var.backup_endpoint != "" ? var.backup_endpoint : null
  }

  skip_credentials_validation = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true
}

provider "zerotier" {
  alias    = "this"
  for_each = local.use_zerotier ? local.on : local.off

  zerotier_central_token = var.zerotier_api_token
}

provider "google" {
  alias    = "this"
  for_each = local.google_needed ? local.on : local.off

  project = var.gcp_project
}

provider "azurerm" {
  alias    = "this"
  for_each = local.azure_needed ? local.on : local.off

  features {}

  subscription_id = var.azure_subscription_id != "" ? var.azure_subscription_id : null
}

provider "ngrok" {
  alias    = "this"
  for_each = local.ngrok_needed ? local.on : local.off

  api_key = var.ngrok_api_key
}

locals {
  build_dir = "${path.root}/../../../.build"

  stack = jsondecode(file("${local.build_dir}/stack.json"))
  slots = local.stack.slots

  use_cloudflare_ingress = local.slots.ingress == "cloudflare"
  use_cloudflare_gate    = local.slots.gate == "cloudflare_access"
  use_cloudflare         = local.use_cloudflare_ingress || local.use_cloudflare_gate
  use_ngrok              = local.slots.ingress == "ngrok"

  # Backup slot dispatch. Declared here because the provider-activation flags
  # below depend on them (splat + one() so a non-r2/s3 slot never indexes a
  # zero-count module).
  s3_backup_ids   = ["s3", "b2", "wasabi", "spaces", "minio", "storj", "linode", "scaleway", "idrive_e2", "filebase", "tigris", "garage", "seaweedfs", "vultr_object"]
  backup_is_r2    = local.slots.backup == "r2"
  backup_is_s3    = contains(local.s3_backup_ids, local.slots.backup)
  backup_is_gcs   = local.slots.backup == "gcs"
  backup_is_azure = local.slots.backup == "azure_blob"

  # Which providers must be configured: exactly the ones a selected slot needs.
  # An unselected provider stays at zero instances and is never configured.
  use_gcp           = local.slots.secrets == "gcp" || local.backup_is_gcs
  use_azure         = local.slots.secrets == "azure_kv" || local.backup_is_azure
  use_zerotier      = local.slots.mesh == "zerotier"
  cloudflare_needed = local.use_cloudflare || local.backup_is_r2
  tailscale_needed  = local.manage_tailscale_policy
  aws_backup_needed = local.backup_is_s3
  google_needed     = local.use_gcp
  azure_needed      = local.use_azure
  ngrok_needed      = local.use_ngrok

  # Only read the Cloudflare zone when a Cloudflare slot is selected, so an
  # ngrok/self-hosted ingress config never needs a Cloudflare token (F2).
  cloudflare_zone_id = one(data.cloudflare_zone.this[*].id)

  # Tailscale policy (D-026). `scripts/mesh-policy.py` merges sealedstack's tags,
  # rules and tests into your imported config/tailscale-policy.hujson and writes
  # the effective JSON below before plan/apply; every other rule is preserved.
  mesh_cfg    = try(local.stack.mesh, {})
  mesh_policy = try(local.mesh_cfg.policy, "managed")
  # `unmanaged` means you paste the block yourself: sealedstack leaves the ACL
  # alone and the mesh module is disabled.
  manage_tailscale_policy = local.slots.mesh == "tailscale" && local.mesh_policy != "unmanaged"
  # Produced by mesh-policy.py. A missing file is a hard, clear error: run the
  # merge step (or `sealedstack init`) before plan/apply.
  tailscale_policy = local.manage_tailscale_policy ? file("${local.build_dir}/tailscale-policy.json") : "{}"

  apps = {
    for file in fileset("${local.build_dir}/apps", "*.json") :
    trimsuffix(file, ".json") => jsondecode(file("${local.build_dir}/apps/${file}"))
  }

  deployable = {
    for name, app in local.apps : name => app if !app._unresolved
  }

  gated_maps = [
    for name, app in local.deployable :
    app.access != "public" ? { for hostname in app.hostnames : hostname => app.access } : {}
  ]
  gated = length(local.gated_maps) > 0 ? merge(local.gated_maps...) : {}

  bypass_maps = [
    for name, app in local.deployable :
    try(length(app.manifest.user_auth.webhook_paths), 0) > 0 ? {
      for hostname in app.hostnames : hostname => app.manifest.user_auth.webhook_paths
    } : {}
  ]
  bypass = length(local.bypass_maps) > 0 ? merge(local.bypass_maps...) : {}

  # Public hostnames under the catch-all that need an explicit bypass (H4).
  public_hostnames = flatten([
    for name, app in local.deployable :
    app.access == "public" ? [for hostname in app.hostnames : hostname] : []
  ])

  redirects = flatten([
    for name, app in local.deployable : [
      for source, target in try(app.redirects, {}) : { source = source, target = target }
    ]
  ])

  # Defensively drop empty / bare-`*` / `**` entries: `ends_with(uri, "")` is
  # always true and would block all traffic. scripts/validate.py rejects them
  # too; this also guards a stale or hand-edited .build.
  blocked = [
    for path in local.stack.edge_security.blocked_paths :
    path if length(trimspace(trim(path, "*"))) > 0
  ]
  waf_terms = [
    for path in local.blocked :
    startswith(path, "*")
    ? "ends_with(http.request.uri.path, \"${substr(path, 1, length(path) - 1)}\")"
    : "http.request.uri.path contains \"${path}\""
  ]
  waf_expression = length(local.waf_terms) > 0 ? "(${join(" or ", local.waf_terms)})" : ""

  # Response security headers on every app hostname (D-078). Cloudflare's own
  # Access login page (the team domain) is not ours to change.
  security_headers_on = try(local.stack.edge_security.security_headers, true)
  app_hostnames       = sort(distinct(flatten([for name, app in local.deployable : app.hostnames])))
  security_headers_expression = format(
    "(http.host in {%s})",
    join(" ", [for hostname in local.app_hostnames : "\"${hostname}\""]),
  )

  backup_bucket = (
    local.backup_is_r2 ? one(module.backup_r2[*].bucket) :
    local.backup_is_s3 ? one(module.backup_s3[*].bucket) :
    local.backup_is_gcs ? one(module.backup_gcs[*].bucket) :
    local.backup_is_azure ? one(module.backup_azure[*].bucket) : ""
  )
  backup_endpoint = (
    local.backup_is_r2 ? one(module.backup_r2[*].endpoint) :
    local.backup_is_s3 ? var.backup_endpoint :
    local.backup_is_gcs ? one(module.backup_gcs[*].endpoint) :
    local.backup_is_azure ? one(module.backup_azure[*].endpoint) : ""
  )
  backup_region = local.backup_is_r2 ? "auto" : var.backup_region

  # secrets slot dispatch: the tunnel/connector token comes from whichever
  # ingress implementation is selected.
  ingress_connector_tokens = local.use_ngrok ? {
    for name, ingress in module.ingress_ngrok : name => ingress.connector_token
    } : {
    for name, ingress in module.ingress : name => ingress.tunnel_token
  }
}

data "cloudflare_zone" "this" {
  count    = local.use_cloudflare ? 1 : 0
  provider = cloudflare.this["default"]

  filter = {
    name = local.stack.cloudflare.zone
  }
}

# --- mesh (dispatch on slots.mesh) ------------------------------------------

module "mesh_tailscale" {
  source = "../../modules/mesh_tailscale"
  count  = local.manage_tailscale_policy ? 1 : 0

  providers = {
    tailscale = tailscale.this["default"]
  }

  policy = local.tailscale_policy
}

module "mesh_zerotier" {
  source = "../../modules/mesh_zerotier"
  count  = local.slots.mesh == "zerotier" ? 1 : 0

  providers = {
    zerotier = zerotier.this["default"]
  }

  name        = "sealedstack"
  private     = true
  assign_ipv4 = true
  servers     = { for name, member_id in var.zerotier_members : name => { member_id = member_id } }
}

# --- gate (before ingress: the tunnel needs the Access audience tags) --------

module "gate" {
  source = "../../modules/gate_cloudflare_access"
  count  = local.use_cloudflare_gate ? 1 : 0

  providers = {
    cloudflare = cloudflare.this["default"]
  }

  account_id       = local.stack.cloudflare.account_id
  zone_id          = local.cloudflare_zone_id
  domain           = local.stack.domain
  policies         = local.stack.gate.policies
  gated            = local.gated
  catch_all        = local.stack.gate.catch_all
  bypass           = local.bypass
  public_hostnames = local.public_hostnames
}

# --- ingress (one tunnel per app) -------------------------------------------

module "ingress" {
  source   = "../../modules/ingress_cloudflare"
  for_each = { for k, v in local.deployable : k => v if local.use_cloudflare_ingress }

  providers = {
    cloudflare = cloudflare.this["default"]
  }

  account_id      = local.stack.cloudflare.account_id
  zone_id         = local.cloudflare_zone_id
  app             = each.key
  hostnames       = each.value.hostnames
  gated           = each.value.access != "public"
  team_name       = local.stack.cloudflare.team_name
  aud_by_hostname = local.use_cloudflare_gate ? module.gate[0].aud_by_hostname : {}
}

module "ingress_ngrok" {
  source   = "../../modules/ingress_ngrok"
  for_each = { for k, v in local.deployable : k => v if local.use_ngrok }

  providers = {
    ngrok = ngrok.this["default"]
  }

  app       = each.key
  hostnames = each.value.hostnames
  gated     = each.value.access != "public"
}

# --- edge security ----------------------------------------------------------

resource "cloudflare_ruleset" "waf" {
  count    = local.use_cloudflare && length(local.waf_terms) > 0 ? 1 : 0
  provider = cloudflare.this["default"]

  zone_id = local.cloudflare_zone_id
  name    = "sealedstack-waf"
  kind    = "zone"
  phase   = "http_request_firewall_custom"

  rules = [{
    action      = "block"
    description = "block-scanner-paths"
    enabled     = true
    expression  = local.waf_expression
  }]
}

resource "cloudflare_bot_management" "this" {
  count    = local.use_cloudflare ? 1 : 0
  provider = cloudflare.this["default"]

  zone_id            = local.cloudflare_zone_id
  fight_mode         = local.stack.edge_security.bot_fight_mode
  ai_bots_protection = local.stack.edge_security.block_ai_bots ? "block" : "disabled"
  enable_js          = true
}

resource "cloudflare_ruleset" "redirects" {
  count    = local.use_cloudflare && length(local.redirects) > 0 ? 1 : 0
  provider = cloudflare.this["default"]

  zone_id = local.cloudflare_zone_id
  name    = "sealedstack-redirects"
  kind    = "zone"
  phase   = "http_request_dynamic_redirect"

  rules = [
    for redirect in local.redirects : {
      action      = "redirect"
      description = "redirect ${redirect.source} to ${redirect.target}"
      enabled     = true
      expression  = "http.host eq \"${redirect.source}\""
      action_parameters = {
        from_value = {
          status_code           = 301
          preserve_query_string = true
          target_url = {
            expression = "concat(\"https://${redirect.target}\", http.request.uri.path)"
          }
        }
      }
    }
  ]
}

# Headers only: no Content-Security-Policy, because a policy that is right for
# one app (inline scripts, CDNs, frames) breaks another. Apps send their own.
resource "cloudflare_ruleset" "security_headers" {
  count    = local.use_cloudflare && local.security_headers_on && length(local.app_hostnames) > 0 ? 1 : 0
  provider = cloudflare.this["default"]

  zone_id = local.cloudflare_zone_id
  name    = "sealedstack-security-headers"
  kind    = "zone"
  phase   = "http_response_headers_transform"

  rules = [{
    action      = "rewrite"
    description = "security-response-headers"
    enabled     = true
    expression  = local.security_headers_expression
    action_parameters = {
      headers = {
        "Permissions-Policy"     = { operation = "set", value = "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()" }
        "X-Content-Type-Options" = { operation = "set", value = "nosniff" }
        "Referrer-Policy"        = { operation = "set", value = "strict-origin-when-cross-origin" }
      }
    }
  }]
}

# --- backup slot ------------------------------------------------------------

module "backup_r2" {
  source = "../../modules/backup_r2"
  count  = local.slots.backup == "r2" ? 1 : 0

  providers = {
    cloudflare = cloudflare.this["default"]
  }

  account_id = local.stack.cloudflare.account_id
  bucket     = "sealedstack-backups"
}

module "backup_s3" {
  source = "../../modules/backup_s3"
  count  = local.backup_is_s3 ? 1 : 0

  providers = {
    aws = aws.backup["default"]
  }

  bucket   = "sealedstack-backups"
  endpoint = var.backup_endpoint
}

module "backup_gcs" {
  source = "../../modules/backup_gcs"
  count  = local.backup_is_gcs ? 1 : 0

  providers = {
    google = google.this["default"]
  }

  bucket   = "sealedstack-backups"
  project  = var.gcp_project
  location = var.backup_location
}

module "backup_azure" {
  source = "../../modules/backup_azure"
  count  = local.backup_is_azure ? 1 : 0

  providers = {
    azurerm = azurerm.this["default"]
  }

  container       = "sealedstack-backups"
  storage_account = var.azure_storage_account
  resource_group  = var.azure_resource_group
}

# --- secrets slot (write tunnel tokens; spec section 6) ---------------------
