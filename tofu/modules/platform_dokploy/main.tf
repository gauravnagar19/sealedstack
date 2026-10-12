# platform_dokploy (spec section 9, D-009/D-010/D-011/D-018): a project per app,
# one compose deployment and one tunnel connector per app × server, domain
# routing and, when needed, a managed Postgres with scheduled backups.

resource "dokploy_project" "this" {
  name        = var.name
  description = "sealedstack: ${var.name}"
}

locals {
  env_id          = dokploy_project.this.production_environment_id
  has_db          = var.database != null
  has_external_db = var.external_database != null
  has_redis       = var.redis != null
  # Only tunnel-based ingress (cloudflare, ngrok) needs a connector; self-hosted
  # ingress (caddy/traefik/nginx/frp/pangolin/zrok/bore) does not (M6).
  has_connector = contains(["cloudflare", "ngrok"], var.connector_type)

  image_compose = yamlencode({
    services = {
      web = {
        image    = var.image
        restart  = "unless-stopped"
        networks = ["dokploy-network"]
      }
    }
    networks = { "dokploy-network" = { external = true } }
  })

  connector_compose = var.connector_type == "ngrok" ? yamlencode({
    services = {
      connector = {
        image       = var.ngrok_connector_image
        command     = ["http", "dokploy-traefik:80", "--log", "stdout"]
        restart     = "unless-stopped"
        environment = { NGROK_AUTHTOKEN = "$${NGROK_AUTHTOKEN}" }
        networks    = ["dokploy-network"]
      }
    }
    networks = { "dokploy-network" = { external = true } }
    }) : yamlencode({
    services = {
      connector = {
        image       = var.connector_image
        command     = "tunnel --no-autoupdate run"
        restart     = "unless-stopped"
        read_only   = true
        cap_drop    = ["ALL"]
        environment = { TUNNEL_TOKEN = "$${TUNNEL_TOKEN}" }
        networks    = ["dokploy-network"]
      }
    }
    networks = { "dokploy-network" = { external = true } }
  })
}

resource "dokploy_compose" "app" {
  for_each = var.source_type == "repo" ? toset(var.servers) : toset([])

  name           = var.name
  environment_id = local.env_id
  server_id      = lookup(var.server_ids, each.key, null)
  compose_type   = "docker-compose"
  compose_path   = var.build_mode == "server" ? var.compose_path : null
  auto_deploy    = false # releases are owned by the app's CI (D-015)
  # ci mode: Dokploy never clones or builds; it pulls the rendered deploy compose.
  raw = var.build_mode == "ci" && var.deploy_compose != "" ? { compose_file = var.deploy_compose } : null
  # server mode: Dokploy clones the repo at ref and builds.
  git = var.build_mode == "server" ? { url = "https://github.com/${var.repo}.git", branch = var.ref } : null

  lifecycle {
    # The app's CI owns releases (D-015/D-032): never reset the released compose
    # file (ci) or Git branch (server) on apply (N9). apply seeds both once.
    ignore_changes = [raw, git]
  }
}

# Image-only apps have a static, infra-owned compose: apply manages the image, so
# it must NOT be under ignore_changes.
resource "dokploy_compose" "image" {
  for_each = var.source_type == "image" ? toset(var.servers) : toset([])

  name           = var.name
  environment_id = local.env_id
  server_id      = lookup(var.server_ids, each.key, null)
  compose_type   = "docker-compose"
  auto_deploy    = false # releases are owned by the app's CI (D-015)
  raw            = { compose_file = local.image_compose }
}

locals {
  # The app's compose per server, whichever flavour created it.
  compose = merge(
    { for server, resource in dokploy_compose.app : server => resource },
    { for server, resource in dokploy_compose.image : server => resource },
  )
}

resource "dokploy_domain" "this" {
  for_each = {
    for pair in setproduct(var.servers, var.hostnames) : "${pair[0]}|${pair[1]}" => pair
  }

  compose_id   = local.compose[each.value[0]].id
  host         = each.value[1]
  port         = var.web_port
  service_name = var.web_service
  https        = false # TLS ends at the Cloudflare edge
  path         = "/"
}

resource "dokploy_compose" "connector" {
  for_each = local.has_connector ? toset(var.servers) : toset([])

  name           = "${var.name}-connector"
  environment_id = local.env_id
  server_id      = lookup(var.server_ids, each.key, null)
  compose_type   = "docker-compose"
  auto_deploy    = false
  raw            = { compose_file = local.connector_compose }
}

resource "dokploy_environment_variables" "connector" {
  for_each = local.has_connector ? toset(var.servers) : toset([])

  compose_id = dokploy_compose.connector[each.key].id
  variables = var.connector_type == "ngrok" ? {
    NGROK_AUTHTOKEN = var.connector_token
    } : {
    TUNNEL_TOKEN = var.connector_token
  }
}

# --- managed database (spec section 11, D-018) ------------------------------

resource "random_password" "db" {
  count = local.has_db ? 1 : 0

  length  = 32
  special = false
}

resource "dokploy_postgres" "this" {
  count = local.has_db ? 1 : 0

  name              = var.database.name
  environment_id    = local.env_id
  server_id         = lookup(var.server_ids, var.servers[0], null)
  database_name     = var.database.database
  database_user     = var.database.user
  database_password = random_password.db[0].result
  docker_image      = "postgres:${var.database.version}"

  lifecycle {
    # Never let a render hiccup or a config mistake destroy a live database (C2).
    prevent_destroy = true
  }
}

resource "dokploy_backup" "this" {
  count = local.has_db && var.db_destination_id != "" ? 1 : 0

  service_type      = "postgres"
  service_id        = dokploy_postgres.this[0].id
  database          = var.database.database
  destination_id    = var.db_destination_id
  prefix            = var.db_destination_prefix
  cron_expression   = var.database.cron
  keep_latest_count = var.database.keep
  enabled           = true
}

# 3-2-1: a second copy of every database backup to a second destination.
resource "dokploy_backup" "secondary" {
  count = local.has_db && var.secondary_destination_id != "" ? 1 : 0

  service_type      = "postgres"
  service_id        = dokploy_postgres.this[0].id
  database          = var.database.database
  destination_id    = var.secondary_destination_id
  prefix            = var.db_secondary_prefix
  cron_expression   = var.database.cron
  keep_latest_count = var.database.keep
  enabled           = true
}

# --- volume backups (roadmap: stateful services other than Postgres) ---------
# Dokploy archives the Docker volume as-is, so this covers Redis and any other
# stateful service a logical dump cannot. The manifest names the volume.

resource "dokploy_volume_backup" "this" {
  for_each = { for v in var.volumes : v.volume => v if var.db_destination_id != "" }

  name              = "${var.name}-${each.value.volume}"
  service_type      = "compose"
  service_id        = local.compose[var.servers[0]].id
  service_name      = each.value.service
  volume_name       = each.value.volume
  destination_id    = var.db_destination_id
  prefix            = "${var.name}/volumes/${each.value.volume}/"
  cron_expression   = each.value.cron
  keep_latest_count = each.value.keep
  enabled           = true
}

# Fail loudly when a neon database has no connection string in .build/env.json,
# instead of injecting an empty DATABASE_URL.
resource "terraform_data" "require_external_database" {
  count = local.has_external_db && var.external_database.url == "" ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.external_database.url != ""
      error_message = "needs.postgres.provider is 'neon' but .build/env.json has no value for '${var.external_database.env}'. Put the connection string there (see sealedstack.buildenv) before applying."
    }
  }
}

locals {
  db_url = local.has_db ? format(
    "postgresql://%s:%s@%s:5432/%s",
    var.database.user,
    random_password.db[0].result,
    dokploy_postgres.this[0].app_name,
    var.database.database,
  ) : (local.has_external_db ? var.external_database.url : "")

  redis_url = local.has_redis ? format(
    "redis://:%s@%s:6379",
    random_password.redis[0].result,
    dokploy_redis.this[0].app_name,
  ) : ""

  external_db_env = local.has_external_db ? {
    (var.external_database.env) = var.external_database.url
  } : {}

  app_env = merge(
    var.environment_variables,
    local.has_db ? { (var.database.env) = local.db_url } : {},
    local.external_db_env,
    local.has_redis ? { (var.redis.env) = local.redis_url } : {},
  )
}

resource "random_password" "redis" {
  count = local.has_redis ? 1 : 0

  length  = 32
  special = false
}

resource "dokploy_redis" "this" {
  count = local.has_redis ? 1 : 0

  name              = var.redis.name
  environment_id    = local.env_id
  server_id         = lookup(var.server_ids, var.servers[0], null)
  database_password = random_password.redis[0].result
  docker_image      = "redis:${var.redis.version}"

  lifecycle {
    prevent_destroy = true
  }
}

resource "dokploy_environment_variables" "app" {
  for_each = toset(var.servers)

  compose_id = local.compose[each.key].id
  variables  = local.app_env

  lifecycle {
    # Infra owns these keys but they are MERGED by the env-sync step, which
    # preserves APP_VERSION and unknown keys (D-032). Ignore drift so apply never
    # overwrites the release-owned env (N9).
    ignore_changes = [variables]
  }
}

