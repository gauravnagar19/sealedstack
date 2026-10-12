output "secret_ids" {
  value     = { for key, secret in azurerm_key_vault_secret.this : key => secret.versionless_id }
  sensitive = true
}

output "secret_version_ids" {
  value     = { for key, secret in azurerm_key_vault_secret.this : key => secret.id }
  sensitive = true
}
