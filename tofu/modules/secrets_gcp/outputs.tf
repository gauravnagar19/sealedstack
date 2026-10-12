output "secret_ids" {
  value     = { for key, secret in google_secret_manager_secret.this : key => secret.id }
  sensitive = true
}

output "secret_version_ids" {
  value     = { for key, version in google_secret_manager_secret_version.this : key => version.id }
  sensitive = true
}
