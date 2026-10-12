# compute_hetzner (spec section 4): create a server and a stateful firewall.

resource "hcloud_ssh_key" "this" {
  name       = var.name
  public_key = var.ssh_public_key
}

resource "hcloud_firewall" "this" {
  name = "ss-${var.name}"

  dynamic "rule" {
    for_each = var.firewall_rules
    content {
      direction       = rule.value.direction
      protocol        = rule.value.protocol
      port            = rule.value.port
      source_ips      = rule.value.source_ips
      destination_ips = rule.value.destination_ips
      description     = rule.value.description
    }
  }
}

resource "hcloud_server" "this" {
  name         = var.name
  server_type  = var.server_type
  image        = var.image
  location     = var.location
  ssh_keys     = [hcloud_ssh_key.this.id]
  firewall_ids = [hcloud_firewall.this.id]
  labels       = var.labels
}
