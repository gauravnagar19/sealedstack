# compute_linode (spec section 4): a Linode instance and a cloud firewall.

resource "linode_sshkey" "this" {
  label   = var.name
  ssh_key = var.ssh_public_key
}

resource "linode_firewall" "this" {
  label           = "ss-${var.name}"
  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"

  dynamic "inbound" {
    for_each = var.inbound_rules
    content {
      label    = inbound.value.label
      action   = "ACCEPT"
      protocol = inbound.value.protocol
      ports    = inbound.value.ports
      ipv4     = inbound.value.ipv4
      ipv6     = inbound.value.ipv6
    }
  }

  outbound {
    label    = "all"
    action   = "ACCEPT"
    protocol = "ALL"
    ipv4     = ["0.0.0.0/0"]
    ipv6     = ["::/0"]
  }
}

resource "linode_instance" "this" {
  label           = var.name
  type            = var.server_type
  region          = var.location
  image           = var.image
  authorized_keys = [linode_sshkey.this.ssh_key]
  tags            = ["sealedstack"]
}

resource "linode_firewall_device" "this" {
  firewall_id = linode_firewall.this.id
  entity_id   = tonumber(linode_instance.this.id)
  entity_type = "linode"
}
