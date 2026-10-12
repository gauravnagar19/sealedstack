output "secret_ids" {
  value     = { for key, secret in bitwarden-secrets_secret.this : key => secret.id }
  sensitive = true
}
