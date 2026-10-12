variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "size" {
  type    = string
  default = "s-1vcpu-2gb"
}

variable "region" {
  type    = string
  default = "fra1"
}

variable "image" {
  type    = string
  default = "ubuntu-24-04-x64"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "tags" {
  type    = list(string)
  default = ["sealedstack"]
}

# DigitalOcean's cloud firewall is stateful, so only inbound rules are needed.
variable "inbound_rules" {
  type = list(object({
    protocol         = string
    port_range       = optional(string)
    source_addresses = optional(list(string), ["0.0.0.0/0", "::/0"])
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "tcp", port_range = "22" },
    { protocol = "udp", port_range = "41641" },
    { protocol = "icmp" },
  ]
}
