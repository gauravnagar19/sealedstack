variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "compartment_id" {
  description = "OCI compartment OCID the instance and network are created in."
  type        = string
}

variable "server_type" {
  description = "OCI shape (e.g. VM.Standard.A1.Flex, the free Ampere ARM tier)."
  type        = string
  default     = "VM.Standard.A1.Flex"
}

variable "location" {
  description = "Availability domain (e.g. Uocm:PHX-AD-1). Empty selects the first AD in the compartment."
  type        = string
  default     = ""
}

variable "image" {
  description = "Canonical image name (e.g. Canonical-Ubuntu-24.04) or an image OCID."
  type        = string
  default     = "Canonical-Ubuntu-24.04"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "ocpus" {
  description = "OCPUs for a flexible shape (ignored for fixed shapes)."
  type        = number
  default     = 1
}

variable "memory_in_gbs" {
  description = "Memory in GB for a flexible shape (ignored for fixed shapes)."
  type        = number
  default     = 6
}

variable "vcn_cidr" {
  description = "CIDR for the VCN created for this server."
  type        = string
  default     = "10.0.0.0/16"
}

variable "subnet_cidr" {
  description = "CIDR for the public subnet created for this server."
  type        = string
  default     = "10.0.1.0/24"
}

variable "freeform_tags" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# OCI security lists are stateful: only inbound rules are needed because replies
# to outbound traffic are allowed automatically. Protocols are OCI numbers
# ("6" TCP, "17" UDP, "1" ICMP).
variable "inbound_rules" {
  type = list(object({
    protocol    = string
    port_min    = optional(number)
    port_max    = optional(number)
    icmp_type   = optional(number)
    icmp_code   = optional(number)
    source      = optional(string, "0.0.0.0/0")
    description = optional(string)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "6", port_min = 22, port_max = 22, description = "SSH bootstrap; closed by the host firewall after site.yml" },
    { protocol = "17", port_min = 41641, port_max = 41641, description = "Tailscale direct path" },
    { protocol = "1", description = "ICMP and PMTU" },
  ]
}
