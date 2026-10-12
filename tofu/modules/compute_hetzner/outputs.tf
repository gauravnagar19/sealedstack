output "ip" {
  description = "Public IPv4 address of the new server."
  value       = hcloud_server.this.ipv4_address
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
