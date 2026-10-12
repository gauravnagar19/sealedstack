variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "plan" {
  description = "Hostinger VPS plan identifier (see the hPanel, e.g. kvm1)."
  type        = string
}

variable "template_id" {
  description = "OS template id (Ubuntu 24.04). Use data.hostinger_vps_templates to look it up."
  type        = string
}

variable "data_center_id" {
  description = "Data centre id (see data.hostinger_vps_data_centers)."
  type        = string
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}
