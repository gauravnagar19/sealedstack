output "project_id" {
  value = dokploy_project.this.id
}

output "compose_ids" {
  value = { for server, compose in local.compose : server => compose.id }
}

output "app_env" {
  description = "Infra-owned env keys (database/redis/external/object-storage/secrets) merged into the compose by the env-sync step (D-032)."
  value       = local.app_env
  sensitive   = true
}

output "connector_ids" {
  value = { for server, compose in dokploy_compose.connector : server => compose.id }
}

output "database_id" {
  value = var.database == null ? null : dokploy_postgres.this[0].id
}

output "database_name" {
  value = var.database == null ? null : var.database.database
}

output "database_url" {
  description = "Connection string for the managed database, if any."
  value       = local.db_url
  sensitive   = true
}
