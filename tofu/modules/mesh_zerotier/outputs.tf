output "network_id" {
  description = "The ZeroTier network (NetworkID) the servers join."
  value       = zerotier_network.this.id
}

output "members" {
  description = "Member resource ids keyed by server name."
  value       = { for name, member in zerotier_member.this : name => member.id }
}

output "ipv4_by_server" {
  description = "ZeroTier-assigned IPv4 addresses keyed by server name (empty until assigned)."
  value       = { for name, member in zerotier_member.this : name => member.ipv4_assignments }
}
