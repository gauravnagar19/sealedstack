output "secret_paths" {
  value     = { for key, secret in vault_kv_secret_v2.this : key => secret.path }
  sensitive = true
}
