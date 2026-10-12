# mesh_zerotier (spec section 4): a central ZeroTier controller network with
# one member per server, so `just server-up` nodes appear on the private mesh.
# The network is managed here; hosts join it via the ZeroTier client and their
# node address is fed back through `var.servers`.

resource "zerotier_network" "this" {
  name        = var.name
  description = var.description
  private     = var.private

  dynamic "assign_ipv4" {
    for_each = var.assign_ipv4 ? [1] : []
    content {
      zerotier = true
    }
  }

  dynamic "dns" {
    for_each = length(var.dns_servers) > 0 ? [1] : []
    content {
      domain  = var.dns_domain
      servers = var.dns_servers
    }
  }
}

resource "zerotier_member" "this" {
  for_each = var.servers

  network_id         = zerotier_network.this.id
  member_id          = each.value.member_id
  name               = each.key
  description        = coalesce(each.value.description, "sealedstack ${each.key}")
  authorized         = each.value.authorized
  hidden             = each.value.hidden
  no_auto_assign_ips = each.value.no_auto_assign_ips
}
