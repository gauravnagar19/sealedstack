variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  description = "Civo instance size (e.g. g3.xsmall)."
  type        = string
  default     = "g3.xsmall"
}

variable "location" {
  description = "Civo region (e.g. LON1)."
  type        = string
  default     = "LON1"
}

variable "image" {
  description = "Civo disk image (e.g. ubuntu-noble)."
  type        = string
  default     = "ubuntu-noble"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "tags" {
  type    = list(string)
  default = ["sealedstack"]
}

# Civo's firewall is stateful; only inbound rules are needed. Unlike most
# providers the rules are inlined on the firewall resource.
variable "inbound_rules" {
  type = list(object({
    protocol   = string
    port_range = optional(string)
    cidr       = optional(list(string), ["0.0.0.0/0"])
    label      = optional(string)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "tcp", port_range = "22", label = "SSH bootstrap; closed by the host firewall after site.yml" },
    { protocol = "udp", port_range = "41641", label = "Tailscale direct path" },
    { protocol = "icmp", label = "ICMP and PMTU" },
  ]
}
