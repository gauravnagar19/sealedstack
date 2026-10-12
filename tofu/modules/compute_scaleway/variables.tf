variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  type    = string
  default = "DEV1-S"
}

variable "location" {
  description = "Scaleway zone slug (e.g. fr-par-1)."
  type        = string
  default     = "fr-par-1"
}

variable "image" {
  description = "Scaleway image name (e.g. ubuntu_noble)."
  type        = string
  default     = "ubuntu_noble"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

# Scaleway security groups are stateful; only inbound rules are needed. The
# default policy drops everything not explicitly accepted, and the built-in
# default rules (which open SSH) are disabled.
variable "inbound_rules" {
  type = list(object({
    protocol = string
    port     = optional(number)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "TCP", port = 22 },
    { protocol = "UDP", port = 41641 },
    { protocol = "ICMP" },
  ]
}
