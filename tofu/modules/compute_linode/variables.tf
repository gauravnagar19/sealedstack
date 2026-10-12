variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  type    = string
  default = "g6-nanode-1"
}

variable "location" {
  description = "Linode region slug (e.g. eu-central)."
  type        = string
  default     = "eu-central"
}

variable "image" {
  description = "Linode image id (e.g. linode/ubuntu24.04)."
  type        = string
  default     = "linode/ubuntu24.04"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

# Linode's cloud firewall is stateful, so only inbound rules are needed: replies
# to outbound traffic are allowed automatically. The inbound policy drops
# everything that is not explicitly accepted.
variable "inbound_rules" {
  type = list(object({
    label    = string
    protocol = string
    ports    = optional(string)
    ipv4     = optional(list(string), ["0.0.0.0/0"])
    ipv6     = optional(list(string), ["::/0"])
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { label = "ssh-bootstrap", protocol = "TCP", ports = "22" },
    { label = "tailscale-direct", protocol = "UDP", ports = "41641" },
    { label = "icmp", protocol = "ICMP" },
  ]
}
