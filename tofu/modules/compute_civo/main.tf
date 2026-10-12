# compute_civo (spec section 4): an instance and a firewall.

resource "civo_ssh_key" "this" {
  name       = var.name
  public_key = var.ssh_public_key
}

# Civo's firewall is stateful (egress is allowed), and create_default_rules is
# disabled so the provider does not open SSH/HTTP/HTTPS: only the rules below
# are accepted, everything else inbound is dropped.
resource "civo_firewall" "this" {
  name                 = "ss-${var.name}"
  region               = var.location
  create_default_rules = false

  dynamic "ingress_rule" {
    for_each = var.inbound_rules
    content {
      action     = "allow"
      protocol   = ingress_rule.value.protocol
      port_range = ingress_rule.value.port_range
      cidr       = ingress_rule.value.cidr
      label      = ingress_rule.value.label
    }
  }
}

resource "civo_instance" "this" {
  hostname    = var.name
  size        = var.server_type
  region      = var.location
  disk_image  = var.image
  firewall_id = civo_firewall.this.id
  sshkey_id   = civo_ssh_key.this.id
  tags        = var.tags
}
