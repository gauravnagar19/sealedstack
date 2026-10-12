# compute_hostinger (spec section 4): create a VPS and install the operator key.
# Hostinger exposes no provider firewall through its API, so origin protection
# relies on UFW + the DOCKER-USER block on the host (spec section 13).

resource "hostinger_vps_ssh_key" "this" {
  name = var.name
  key  = var.ssh_public_key
}

resource "hostinger_vps" "this" {
  hostname       = var.name
  plan           = var.plan
  template_id    = var.template_id
  data_center_id = var.data_center_id
  ssh_key_ids    = [hostinger_vps_ssh_key.this.id]
}
