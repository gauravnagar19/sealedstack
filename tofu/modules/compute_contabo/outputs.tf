output "ip" {
  description = "Empty: the server already exists (the operator passes the IP)."
  value       = ""
}

output "managed" {
  description = "Whether sealedstack created the server."
  value       = false
}

output "firewall" {
  description = "How the provider firewall is applied."
  value       = "api"
}
