# compute_ovh (spec section 4): an OVHcloud Public Cloud instance and a security
# group.
#
# flavor_id and image_id are opaque per-region identifiers, so both are resolved
# from the project catalog by name (server_type / image).

data "ovh_cloud_instance_flavors" "this" {
  service_name = var.service_name
  region       = var.location
}

data "ovh_cloud_instance_images" "this" {
  service_name = var.service_name
  region       = var.location
}

resource "ovh_cloud_ssh_key" "this" {
  service_name = var.service_name
  name         = var.name
  public_key   = var.ssh_public_key
}

# OVHcloud security groups are region-scoped and stateful: only inbound rules
# are needed. Egress is allowed by OpenStack's default rule, which is not part
# of the managed rule set. protocols: TCP, UDP, ICMP.
resource "ovh_cloud_security_group" "this" {
  service_name = var.service_name
  region       = var.location
  name         = "ss-${var.name}"
  description  = "sealedstack ${var.name}: mesh + icmp in"

  rule = [for rule in var.inbound_rules : {
    direction        = "INGRESS"
    ethernet_type    = "IPV4"
    protocol         = rule.protocol
    port_range_min   = rule.port_min
    port_range_max   = rule.port_max
    remote_ip_prefix = rule.remote_ip_prefix
    description      = rule.description
  }]
}

resource "ovh_cloud_instance" "this" {
  service_name = var.service_name
  region       = var.location
  name         = var.name

  flavor_id = one([
    for flavor in data.ovh_cloud_instance_flavors.this.flavors :
    flavor.id if flavor.name == var.server_type
  ])

  image_id = one([
    for image in data.ovh_cloud_instance_images.this.images :
    image.id if image.name == var.image
  ])

  ssh_key_name = ovh_cloud_ssh_key.this.name

  security_group_ids = [ovh_cloud_security_group.this.id]

  networks = [
    { auto_assign_public_ip = true },
  ]
}
