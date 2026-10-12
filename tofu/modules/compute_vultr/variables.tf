variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "plan" {
  type    = string
  default = "vc2-1c-2gb"
}

variable "region" {
  type    = string
  default = "fra"
}

variable "os_id" {
  description = "Vultr OS id (Ubuntu 24.04)."
  type        = number
  default     = 2284
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

# Vultr firewall rules are stateful; only inbound rules are needed.
variable "inbound_rules" {
  type = list(object({
    protocol    = string
    port        = optional(string)
    subnet      = optional(string, "0.0.0.0")
    subnet_size = optional(number, 0)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "tcp", port = "22" },
    { protocol = "udp", port = "41641" },
  ]
}
