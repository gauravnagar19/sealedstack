output "ip" {
  description = "Public IPv4 address of the new server."
  value       = [for ip in scaleway_instance_server.this.public_ips : ip.address if ip.family == "inet"][0]
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
