output "ip" {
  description = "Public IPv4 address of the new server."
  value       = one([for ni in upcloud_server.this.network_interface : ni.ip_address if ni.type == "public"])
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
