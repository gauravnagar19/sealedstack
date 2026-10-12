output "ip" {
  description = "Public IPv4 address of the new instance."
  value       = civo_instance.this.public_ip
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
