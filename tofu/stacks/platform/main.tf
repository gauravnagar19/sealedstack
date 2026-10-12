# platform stack (spec sections 9 and 11): one project, compose deployment,
# domain and tunnel connector per app × server, a managed Postgres with
# scheduled backups, and worker registration (v0.4).

# `slots.platform` selects the platform implementation (spec §4). Only Dokploy
# has a native OpenTofu module today; the other implementations are Ansible-only,
# so when `slots.platform` is not `dokploy` this stack configures no Dokploy
# provider and creates nothing.
#
# Provider instances are declared as *aliased* configurations with `for_each`:
# an unselected provider has zero instances and is therefore never configured
# (no inert placeholder values). Every resource and module selects the single
# active instance explicitly as `<provider>.<alias>["default"]`.
locals {
  on  = { default = true }
  off = {}
}

locals {
  build_dir           = "${path.root}/../../../.build"
  stack_path          = "${local.build_dir}/stack.json"
  stack               = try(jsondecode(file(local.stack_path)), {})
  platform_impl       = try(local.stack.slots.platform, "")
  platform_is_dokploy = local.platform_impl == "dokploy"
}

provider "dokploy" {
  alias    = "this"
  for_each = local.platform_is_dokploy ? local.on : local.off

  endpoint = var.dokploy_endpoint
  api_key  = var.dokploy_api_key
}

# Only R2 buckets for `needs.object_storage` are managed here. The token comes
# from var.cloudflare_api_token, falling back to CLOUDFLARE_API_TOKEN in the
# environment (passed into the toolbox by `just`); the account id comes from
# var.cloudflare_account_id, falling back to the rendered stack model below.
provider "cloudflare" {
  alias    = "this"
  for_each = local.cloudflare_needed ? local.on : local.off

  # null lets the provider read CLOUDFLARE_API_TOKEN from the environment.
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : null
}

locals {
  apps = {
    for file in fileset("${local.build_dir}/apps", "*.json") :
    trimsuffix(file, ".json") => jsondecode(file("${local.build_dir}/apps/${file}"))
  }

  deployable = {
    for name, app in local.apps : name => app if !app._unresolved
  }

  edge_path   = "${local.build_dir}/edge-outputs.json"
  edge        = fileexists(local.edge_path) ? jsondecode(file(local.edge_path)) : { connector_tokens = {} }
  edge_backup = try(local.edge.backup, { bucket = "", endpoint = "", region = "auto" })

  env_path = "${local.build_dir}/env.json"
  env      = fileexists(local.env_path) ? jsondecode(file(local.env_path)) : {}

  # `local.stack` (the rendered stack model) is read above; used here for
  # backup.secondary.
  backup_secondary = try(local.stack.backup.secondary, { endpoint = "", region = "", bucket = "" })

  # needs.postgres -> module database object (spec section 5.4). A manifest may
  # select an external provider (`provider: neon`), in which case the platform
  # creates no dokploy_postgres and injects the connection string it finds in
  # .build/env.json (roadmap: managed Postgres option).
  postgres_needs = {
    for name, app in local.deployable :
    name => try(app.manifest.needs.postgres, null)
  }

  databases = {
    for name, pg in local.postgres_needs :
    name => {
      name              = "${name}-db"
      database          = pg.database
      user              = name
      env               = pg.env
      version           = pg.version
      cron              = lookup(local.backup_cron, try(pg.backup.every, "6h"), "0 */6 * * *")
      keep              = try(pg.backup.keep, 28)
      restore_on_create = try(pg.restore_on_create, null) != null
    }
    if pg != null && try(pg.provider, "dokploy") == "dokploy"
  }
  any_databases = length(local.databases) > 0

  external_databases = {
    for name, pg in local.postgres_needs :
    name => pg == null || try(pg.provider, "dokploy") != "neon" ? null : {
      env = pg.env
      url = lookup(lookup(local.env, name, {}), pg.env, "")
    }
  }

  # needs.redis -> module redis object.
  redises = {
    for name, app in local.deployable :
    name => try(app.manifest.needs.redis, null) == null ? null : {
      name    = "${name}-redis"
      version = try(app.manifest.needs.redis.version, "7")
      env     = app.manifest.needs.redis.env
    }
  }

  # needs.volumes -> scheduled archives (roadmap: volume backups).
  volumes = {
    for name, app in local.deployable :
    name => [
      for v in try(app.manifest.needs.volumes, []) : {
        volume  = v.volume
        service = v.service
        cron    = lookup(local.backup_cron, try(v.every, "1d"), "0 3 * * *")
        keep    = try(v.keep, 14)
      }
    ]
  }
  any_volumes = length(flatten(values(local.volumes))) > 0
  any_backups = local.any_databases || local.any_volumes

  # needs.object_storage -> one R2 bucket per app + S3 env vars (spec section
  # 5.4). Scoped R2 tokens are not managed by OpenTofu (Q1), so the app reuses
  # the R2 account credentials already passed for the backup destination; a
  # per-bucket scoped token is a documented follow-up (docs/operations.md).
  object_storage_needs = {
    for name, app in local.deployable :
    name => try(app.manifest.needs.object_storage, null)
  }
  object_storage = {
    for name, os in local.object_storage_needs :
    name => {
      bucket = try(os.bucket, "sealedstack-${name}")
      region = try(os.region, "auto")
      prefix = try(os.prefix, "")
      env    = os.env
    }
    if os != null
  }

  # The cloudflare provider is needed only when R2 buckets are actually managed.
  cloudflare_needed = local.platform_is_dokploy && length(local.object_storage) > 0

  # R2 account: an explicit variable wins, else the rendered stack model.
  r2_account_id = var.cloudflare_account_id != "" ? var.cloudflare_account_id : try(local.stack.cloudflare.account_id, "")

  # A dedicated object-storage S3 key can be supplied; otherwise reuse the one
  # already configured for the backup destination (the same R2 account).
  object_storage_access_key_id     = var.object_storage_access_key_id != "" ? var.object_storage_access_key_id : var.backup_access_key_id
  object_storage_secret_access_key = var.object_storage_secret_access_key != "" ? var.object_storage_secret_access_key : var.backup_secret_access_key

  object_storage_vars = {
    for name, os in local.object_storage :
    name => merge(
      {
        "${os.env}_ENDPOINT"          = "https://${local.r2_account_id}.r2.cloudflarestorage.com"
        "${os.env}_REGION"            = os.region
        "${os.env}_BUCKET"            = os.bucket
        "${os.env}_ACCESS_KEY_ID"     = local.object_storage_access_key_id
        "${os.env}_SECRET_ACCESS_KEY" = local.object_storage_secret_access_key
      },
      os.prefix == "" ? {} : { "${os.env}_PREFIX" = os.prefix },
    )
  }
}

# --- object storage (spec section 5.4, needs.object_storage) ----------------
# One R2 bucket per app that declares needs.object_storage. The S3 credentials
# are shared (Q1: OpenTofu does not manage R2 API tokens); see
# docs/operations.md for the manual scoping steps.

resource "cloudflare_r2_bucket" "app" {
  for_each = local.platform_is_dokploy ? local.object_storage : {}
  provider = cloudflare.this["default"]

  account_id = local.r2_account_id
  name       = each.value.bucket

  lifecycle {
    # App uploads live here; never destroy a bucket because an app was removed
    # from the render. Empty and remove it by hand (docs/operations.md).
    prevent_destroy = true
  }
}

# Fail loudly rather than inject empty S3 credentials into an app that asked
# for object storage.
resource "terraform_data" "require_object_storage" {
  count = local.platform_is_dokploy && length(local.object_storage) > 0 && (
    local.r2_account_id == "" ||
    local.object_storage_access_key_id == "" ||
    local.object_storage_secret_access_key == ""
  ) ? 1 : 0

  lifecycle {
    precondition {
      condition = (
        local.r2_account_id != "" &&
        local.object_storage_access_key_id != "" &&
        local.object_storage_secret_access_key != ""
      )
      error_message = "An app declares needs.object_storage but the R2 account id or S3 credentials are empty. Set cloudflare.account_id in config/stack.yaml and R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY in Secrets Manager (or TF_VAR_object_storage_*)."
    }
  }
}

# --- worker registration (spec section 9.2, D-010, v0.4) --------------------

locals {
  servers = jsondecode(file("${local.build_dir}/servers.json")).servers
  workers = { for name, spec in local.servers : name => spec if spec.role == "worker" }
  server_ids = merge(
    var.server_ids,
    { for name, server in dokploy_server.worker : name => server.id },
  )
}

resource "dokploy_ssh_key" "workers" {
  count    = local.platform_is_dokploy && length(local.workers) > 0 && var.dokploy_ssh_private_key != "" ? 1 : 0
  provider = dokploy.this["default"]

  name        = "sealedstack-workers"
  public_key  = var.dokploy_ssh_public_key
  private_key = var.dokploy_ssh_private_key
}

resource "dokploy_server" "worker" {
  for_each = local.platform_is_dokploy && length(dokploy_ssh_key.workers) > 0 ? local.workers : {}
  provider = dokploy.this["default"]

  name        = each.key
  ip_address  = each.key # reachable over Tailscale by its MagicDNS name
  port        = 22
  username    = var.admin_user
  ssh_key_id  = dokploy_ssh_key.workers[0].id
  server_type = "deploy"
}

# Fail loudly if workers are declared but no Dokploy SSH key is available:
# otherwise their composes would silently land on the control server.
resource "terraform_data" "require_worker_key" {
  count = local.platform_is_dokploy && length(local.workers) > 0 && var.dokploy_ssh_private_key == "" ? 1 : 0

  lifecycle {
    precondition {
      condition     = length(var.dokploy_ssh_private_key) > 0
      error_message = "Worker servers are declared but DOKPLOY_SSH_PRIVATE_KEY is empty. Run `just server-up` (which generates it) or set the key in Bitwarden."
    }
  }
}

# "6h" / "30m" / "1d" -> cron. Precomputed for the handful of values we allow.
locals {
  backup_cron = {
    "5m"  = "*/5 * * * *"
    "15m" = "*/15 * * * *"
    "30m" = "*/30 * * * *"
    "1h"  = "0 * * * *"
    "2h"  = "0 */2 * * *"
    "6h"  = "0 */6 * * *"
    "12h" = "0 */12 * * *"
    "1d"  = "0 3 * * *"
  }
}

# --- backup destination + panel self-backup (spec section 11) ----------------

resource "dokploy_destination" "backups" {
  count    = local.platform_is_dokploy && local.any_backups && local.edge_backup.endpoint != "" ? 1 : 0
  provider = dokploy.this["default"]

  name              = "sealedstack-backups"
  provider_name     = "S3"
  bucket            = local.edge_backup.bucket
  endpoint          = local.edge_backup.endpoint
  region            = local.edge_backup.region
  access_key        = var.backup_access_key_id
  secret_access_key = var.backup_secret_access_key
}

# 3-2-1: a second, off-provider destination (config backup.secondary). The
# credentials come from TF_VAR_backup_secondary_* in the environment.
resource "dokploy_destination" "secondary" {
  count    = local.platform_is_dokploy && local.any_backups && local.backup_secondary.endpoint != "" ? 1 : 0
  provider = dokploy.this["default"]

  name              = "sealedstack-backups-secondary"
  provider_name     = "S3"
  bucket            = local.backup_secondary.bucket
  endpoint          = local.backup_secondary.endpoint
  region            = local.backup_secondary.region
  access_key        = var.backup_secondary_access_key_id
  secret_access_key = var.backup_secondary_secret_access_key
}

resource "dokploy_web_server_backup" "panel" {
  count    = local.platform_is_dokploy && length(dokploy_destination.backups) > 0 ? 1 : 0
  provider = dokploy.this["default"]

  destination_id    = dokploy_destination.backups[0].id
  prefix            = "_dokploy/"
  cron_expression   = "0 4 * * *"
  keep_latest_count = 14
  enabled           = true
}

# --- apps -------------------------------------------------------------------

module "app" {
  source   = "../../modules/platform_dokploy"
  for_each = local.platform_is_dokploy ? local.deployable : {}

  providers = {
    dokploy = dokploy.this["default"]
  }

  name        = each.value.name
  servers     = each.value.servers
  server_ids  = local.server_ids
  hostnames   = each.value.hostnames
  web_service = each.value.web.service
  web_port    = each.value.web.port

  source_type = can(each.value.source.image) ? "image" : "repo"
  image       = try(each.value.source.image, "")
  repo        = try(each.value.source.repo, "")
  ref         = try(each.value.source.ref, "")

  build_mode     = try(each.value._build_mode, "ci")
  deploy_compose = try(each.value._deploy_compose, "")

  environment_variables = merge(
    lookup(local.env, each.key, {}),
    lookup(local.object_storage_vars, each.key, {}),
  )
  connector_token = lookup(local.edge.connector_tokens, each.key, "")
  connector_type  = try(local.edge.ingress_type, "cloudflare")

  database                 = try(local.databases[each.key], null)
  external_database        = local.external_databases[each.key]
  volumes                  = local.volumes[each.key]
  db_destination_id        = length(dokploy_destination.backups) > 0 ? dokploy_destination.backups[0].id : ""
  db_destination_prefix    = "${each.key}/${try(local.databases[each.key].database, "db")}/"
  secondary_destination_id = length(dokploy_destination.secondary) > 0 ? dokploy_destination.secondary[0].id : ""
  db_secondary_prefix      = "${each.key}/${try(local.databases[each.key].database, "db")}/"
  redis                    = local.redises[each.key]
}

# --- registry slot (spec 4.5, D-033) ----------------------------------------
# Register the selected registry with Dokploy so it can pull private images for
# `build.mode: ci` apps (Dokploy never builds in that mode). The rendered config
# comes from scripts/render.py (`registry` slot + providers.registry_<impl>),
# and the read-only pull token from infra.REGISTRY_PULL_TOKEN.
locals {
  registry_path    = "${local.build_dir}/registry.json"
  registry         = fileexists(local.registry_path) ? jsondecode(file(local.registry_path)) : {}
  registry_enabled = try(local.registry.image_prefix, "") != "" && var.registry_pull_token != ""
}

resource "dokploy_registry" "this" {
  count    = local.platform_is_dokploy && local.registry_enabled ? 1 : 0
  provider = dokploy.this["default"]

  name          = "sealedstack-${local.registry.impl}"
  registry_type = "cloud" # Dokploy v0.30 accepts only `cloud` for external registries
  url           = local.registry.url
  username      = local.registry.username
  password      = var.registry_pull_token
  image_prefix  = local.registry.image_prefix
}
