output "ip" {
  description = "Public IPv4 address of the new VPS."
  value       = hostinger_vps.this.ipv4_address
}

output "managed" {
  value = true
}

output "firewall" {
  value = "none"
}
