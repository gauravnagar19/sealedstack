output "ip" {
  description = "Public IPv4 address of the new instance."
  value       = vultr_instance.this.main_ip
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
