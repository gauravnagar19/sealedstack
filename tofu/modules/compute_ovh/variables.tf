variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "service_name" {
  description = "OVHcloud Public Cloud project id (service_name) the instance is created in."
  type        = string
}

variable "server_type" {
  description = "OVHcloud flavor name (e.g. b3-8), resolved to a flavor id in the region."
  type        = string
  default     = "b3-8"
}

variable "location" {
  description = "OVHcloud region (e.g. GRA11)."
  type        = string
  default     = "GRA11"
}

variable "image" {
  description = "OVHcloud image name (e.g. Ubuntu 24.04), resolved to an image id in the region."
  type        = string
  default     = "Ubuntu 24.04"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

# OVHcloud security groups are stateful; only inbound rules are needed.
variable "inbound_rules" {
  type = list(object({
    protocol         = string
    port_min         = optional(number)
    port_max         = optional(number)
    remote_ip_prefix = optional(string, "0.0.0.0/0")
    description      = optional(string)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "TCP", port_min = 22, port_max = 22, description = "SSH bootstrap; closed by the host firewall after site.yml" },
    { protocol = "UDP", port_min = 41641, port_max = 41641, description = "Tailscale direct path" },
    { protocol = "ICMP", description = "ICMP and PMTU" },
  ]
}
