# compute_vultr (spec section 4): an instance and a firewall group.

resource "vultr_ssh_key" "this" {
  name    = var.name
  ssh_key = var.ssh_public_key
}

resource "vultr_firewall_group" "this" {
  description = "ss-${var.name}"
}

resource "vultr_firewall_rule" "this" {
  for_each = { for index, rule in var.inbound_rules : index => rule }

  firewall_group_id = vultr_firewall_group.this.id
  ip_type           = "v4"
  protocol          = each.value.protocol
  port              = each.value.port
  subnet            = each.value.subnet
  subnet_size       = each.value.subnet_size
}

resource "vultr_instance" "this" {
  label             = var.name
  hostname          = var.name
  plan              = var.plan
  region            = var.region
  os_id             = var.os_id
  ssh_key_ids       = [vultr_ssh_key.this.id]
  firewall_group_id = vultr_firewall_group.this.id
}
