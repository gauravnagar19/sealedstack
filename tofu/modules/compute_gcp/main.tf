# compute_gcp (spec section 4): a Compute Engine instance, a VPC and a firewall.

resource "google_compute_network" "this" {
  name                    = "ss-${var.name}"
  auto_create_subnetworks = true
}

resource "google_compute_firewall" "ingress" {
  name      = "ss-${var.name}-ingress"
  network   = google_compute_network.this.name
  direction = "INGRESS"
  priority  = 1000

  source_ranges = ["0.0.0.0/0"]

  dynamic "allow" {
    for_each = var.inbound_rules
    content {
      protocol = allow.value.protocol
      ports    = allow.value.ports
    }
  }
}

resource "google_compute_firewall" "egress" {
  name      = "ss-${var.name}-egress"
  network   = google_compute_network.this.name
  direction = "EGRESS"
  priority  = 1000

  destination_ranges = ["0.0.0.0/0"]

  allow {
    protocol = "all"
  }
}

resource "google_compute_instance" "this" {
  name         = var.name
  machine_type = var.server_type
  zone         = var.location
  tags         = ["sealedstack"]
  labels       = var.labels

  metadata = {
    ssh-keys = "${var.ssh_user}:${var.ssh_public_key}"
  }

  boot_disk {
    initialize_params {
      image = var.image
    }
  }

  network_interface {
    network = google_compute_network.this.id

    # Ephemeral public IP.
    access_config {}
  }
}
