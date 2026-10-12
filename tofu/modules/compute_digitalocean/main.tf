# compute_digitalocean (spec section 4): a droplet and a cloud firewall.

resource "digitalocean_ssh_key" "this" {
  name       = var.name
  public_key = var.ssh_public_key
}

resource "digitalocean_droplet" "this" {
  name     = var.name
  size     = var.size
  region   = var.region
  image    = var.image
  ssh_keys = [digitalocean_ssh_key.this.fingerprint]
  tags     = var.tags
}

resource "digitalocean_firewall" "this" {
  name        = "ss-${var.name}"
  droplet_ids = [digitalocean_droplet.this.id]
  tags        = var.tags

  dynamic "inbound_rule" {
    for_each = var.inbound_rules
    content {
      protocol         = inbound_rule.value.protocol
      port_range       = inbound_rule.value.port_range
      source_addresses = inbound_rule.value.source_addresses
    }
  }

  # Egress stays open; blocked ports are handled by the host firewall.
  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
