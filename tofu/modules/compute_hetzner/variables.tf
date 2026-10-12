variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  type    = string
  default = "cx22"
}

variable "image" {
  type    = string
  default = "ubuntu-24.04"
}

variable "location" {
  type    = string
  default = "fsn1"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "labels" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# Hetzner's firewall is stateful, so only inbound rules are needed: replies to
# outbound traffic are allowed automatically. This differs from the stateless
# rule table in spec section 7.4.
variable "firewall_rules" {
  type = list(object({
    direction       = string
    protocol        = string
    port            = optional(string)
    source_ips      = optional(list(string), ["0.0.0.0/0", "::/0"])
    destination_ips = optional(list(string), [])
    description     = optional(string)
  }))
  default = [
    # Temporary during bootstrap: `server-up` needs SSH on the public IP before
    # the host joins the mesh. site.yml/firewall (UFW + DOCKER-USER) closes 22
    # on the WAN interface afterwards; drop or remove this rule then.
    { direction = "in", protocol = "tcp", port = "22", description = "SSH bootstrap; closed by the host firewall after site.yml" },
    { direction = "in", protocol = "udp", port = "41641", description = "Tailscale direct path" },
    { direction = "in", protocol = "icmp", description = "ICMP and PMTU" },
  ]
}
