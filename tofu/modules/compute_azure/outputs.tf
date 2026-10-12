output "ip" {
  description = "Public IPv4 address of the new VM."
  value       = azurerm_public_ip.this.ip_address
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
