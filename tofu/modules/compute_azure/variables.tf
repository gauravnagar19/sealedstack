variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  description = "Azure VM size."
  type        = string
  default     = "Standard_B1s"
}

variable "location" {
  description = "Azure region (e.g. westeurope)."
  type        = string
  default     = "westeurope"
}

variable "image" {
  description = "Marketplace image URN as publisher:offer:sku:version."
  type        = string
  default     = "Canonical:ubuntu-24_04-lts:server:latest"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "admin_username" {
  description = "VM admin login the operator key is attached to."
  type        = string
  default     = "azureuser"
}

variable "tags" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# Azure NSGs are stateful, so only inbound rules are needed: replies to
# outbound traffic are allowed automatically. The default AllowVNet rule does
# not open the internet. A temporary SSH rule is added for bootstrap and the
# host firewall closes 22 on the WAN interface after site.yml.
variable "inbound_rules" {
  type = list(object({
    name        = string
    protocol    = string
    port_range  = optional(string, "*")
    priority    = number
    description = optional(string)
  }))
  default = [
    { name = "ssh-bootstrap", protocol = "Tcp", port_range = "22", priority = 1000, description = "SSH bootstrap; closed by the host firewall after site.yml" },
    { name = "tailscale-direct", protocol = "Udp", port_range = "41641", priority = 1010, description = "Tailscale direct path" },
    { name = "icmp", protocol = "Icmp", priority = 1020, description = "ICMP and PMTU" },
  ]
}
