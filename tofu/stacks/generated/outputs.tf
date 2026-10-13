output "dokploy_admin_password" {
  value     = random_password.dokploy_admin.result
  sensitive = true
}

output "dokploy_ssh_public_key" {
  value = trimspace(tls_private_key.dokploy_worker.public_key_openssh)
}

output "dokploy_ssh_private_key" {
  value     = tls_private_key.dokploy_worker.private_key_openssh
  sensitive = true
}

output "ci_ssh_public_key" {
  value = trimspace(tls_private_key.ci.public_key_openssh)
}

output "ci_ssh_private_key" {
  value     = tls_private_key.ci.private_key_openssh
  sensitive = true
}

output "values" {
  value     = terraform_data.values.output
  sensitive = true
}
