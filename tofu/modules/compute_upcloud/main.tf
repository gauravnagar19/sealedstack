# compute_upcloud (spec section 4): an UpCloud server and a firewall rule set.

resource "upcloud_server" "this" {
  hostname = var.name
  zone     = var.location
  plan     = var.server_type
  metadata = true
  firewall = true
  labels   = var.labels

  login {
    user = var.ssh_user
    keys = [var.ssh_public_key]
  }

  template {
    storage = var.image
    size    = var.disk_size
  }

  network_interface {
    type = "public"
  }

  network_interface {
    type = "utility"
  }
}

# UpCloud's firewall is stateful, so only inbound rules are needed: replies to
# outbound traffic are allowed automatically. A final default rule drops every
# other inbound packet, which is what seals the origin.
resource "upcloud_firewall_rules" "this" {
  server_id = upcloud_server.this.id

  dynamic "firewall_rule" {
    for_each = var.inbound_rules
    content {
      action                 = "accept"
      direction              = "in"
      protocol               = firewall_rule.value.protocol
      family                 = firewall_rule.value.family
      icmp_type              = firewall_rule.value.icmp_type
      destination_port_start = firewall_rule.value.port_min != null ? tostring(firewall_rule.value.port_min) : null
      destination_port_end   = firewall_rule.value.port_max != null ? tostring(firewall_rule.value.port_max) : null
      comment                = firewall_rule.value.comment
    }
  }

  firewall_rule {
    action    = "drop"
    direction = "in"
    comment   = "Deny all other inbound"
  }

  firewall_rule {
    action    = "accept"
    direction = "out"
    comment   = "Allow all outbound"
  }
}
