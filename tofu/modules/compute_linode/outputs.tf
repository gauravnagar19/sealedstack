output "ip" {
  description = "Public IPv4 address of the new instance."
  value       = tolist(linode_instance.this.ipv4)[0]
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
