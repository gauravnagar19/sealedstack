output "projects" {
  value = { for name, app in module.app : name => app.project_id }
}

output "compose_ids" {
  value = { for name, app in module.app : name => app.compose_ids }
}

output "database_urls" {
  description = "Connection string per app with a managed database."
  value       = { for name, app in module.app : name => app.database_url if app.database_id != null }
  sensitive   = true
}

output "database_names" {
  description = "Database name per app with a managed database."
  value       = { for name, app in module.app : name => app.database_name if app.database_id != null }
}

output "app_env" {
  description = "Infra-owned env keys per app (database/redis/external/object-storage/secrets) merged into Dokploy by the env-sync step (D-032)."
  value       = { for name, app in module.app : name => app.app_env }
  sensitive   = true
}
