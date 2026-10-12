# compute_oracle (spec section 4): an OCI instance, a public VCN/subnet and a
# stateful security list.

locals {
  availability_domain = var.location != "" ? var.location : data.oci_identity_availability_domains.this[0].availability_domains[0].name
  image_id            = startswith(var.image, "ocid1.") ? var.image : data.oci_core_images.this[0].images[0].id
}

data "oci_identity_availability_domains" "this" {
  count          = var.location == "" ? 1 : 0
  compartment_id = var.compartment_id
}

data "oci_core_images" "this" {
  count            = startswith(var.image, "ocid1.") ? 0 : 1
  compartment_id   = var.compartment_id
  operating_system = var.image
  shape            = var.server_type
  sort_by          = "TIMECREATED"
  sort_order       = "DESC"
}

resource "oci_core_vcn" "this" {
  compartment_id = var.compartment_id
  cidr_block     = var.vcn_cidr
  display_name   = "ss-${var.name}"
}

resource "oci_core_internet_gateway" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "ss-${var.name}-igw"
  enabled        = true
}

resource "oci_core_route_table" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "ss-${var.name}-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.this.id
  }
}

resource "oci_core_security_list" "this" {
  compartment_id = var.compartment_id
  vcn_id         = oci_core_vcn.this.id
  display_name   = "ss-${var.name}"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
    description = "All egress"
  }

  dynamic "ingress_security_rules" {
    for_each = var.inbound_rules
    content {
      protocol    = ingress_security_rules.value.protocol
      source      = ingress_security_rules.value.source
      description = ingress_security_rules.value.description

      dynamic "udp_options" {
        for_each = ingress_security_rules.value.protocol == "17" ? [1] : []
        content {
          min = ingress_security_rules.value.port_min
          max = ingress_security_rules.value.port_max
        }
      }

      dynamic "icmp_options" {
        for_each = ingress_security_rules.value.protocol == "1" && ingress_security_rules.value.icmp_type != null ? [1] : []
        content {
          type = ingress_security_rules.value.icmp_type
          code = ingress_security_rules.value.icmp_code
        }
      }
    }
  }
}

resource "oci_core_subnet" "this" {
  compartment_id    = var.compartment_id
  vcn_id            = oci_core_vcn.this.id
  cidr_block        = var.subnet_cidr
  display_name      = "ss-${var.name}-subnet"
  route_table_id    = oci_core_route_table.this.id
  security_list_ids = [oci_core_security_list.this.id]
}

resource "oci_core_instance" "this" {
  compartment_id      = var.compartment_id
  availability_domain = local.availability_domain
  display_name        = var.name
  shape               = var.server_type
  freeform_tags       = var.freeform_tags

  dynamic "shape_config" {
    for_each = can(regex("Flex", var.server_type)) ? [1] : []
    content {
      ocpus         = var.ocpus
      memory_in_gbs = var.memory_in_gbs
    }
  }

  source_details {
    source_type = "image"
    source_id   = local.image_id
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.this.id
    assign_public_ip = true
    display_name     = var.name
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
  }
}
