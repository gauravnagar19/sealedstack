output "ip" {
  description = "Public IPv4 address of the new instance."
  value = try(one(flatten([
    for nic in ovh_cloud_instance.this.current_state.networks :
    [for address in nic.addresses : address.ip if address.version == 4]
  ])), null)
}

output "managed" {
  value = true
}

output "firewall" {
  value = "native"
}
