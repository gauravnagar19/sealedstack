# compute_scaleway (spec section 4): an instance and a stateful security group.

resource "scaleway_account_ssh_key" "this" {
  name       = var.name
  public_key = var.ssh_public_key
}

resource "scaleway_instance_security_group" "this" {
  name                    = "ss-${var.name}"
  zone                    = var.location
  inbound_default_policy  = "drop"
  outbound_default_policy = "accept"
  enable_default_security = false

  dynamic "inbound_rule" {
    for_each = var.inbound_rules
    content {
      action   = "accept"
      protocol = inbound_rule.value.protocol
      port     = inbound_rule.value.port
    }
  }
}

resource "scaleway_instance_server" "this" {
  name              = var.name
  type              = var.server_type
  zone              = var.location
  image             = var.image
  security_group_id = scaleway_instance_security_group.this.id
  tags              = ["sealedstack"]

  depends_on = [scaleway_account_ssh_key.this]
}
