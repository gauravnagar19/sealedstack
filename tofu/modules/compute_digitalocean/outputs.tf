output "ip" {
  description = "Public IPv4 address of the new droplet."
  value       = digitalocean_droplet.this.ipv4_address
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
