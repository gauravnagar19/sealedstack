output "ip" {
  description = "Unchanged; the operator passed the IP on the command line."
  value       = var.ip
}

output "managed" {
  description = "Whether sealedstack created the server."
  value       = false
}

output "firewall" {
  description = "How the provider firewall is applied."
  value       = "manual"
}
