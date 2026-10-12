variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  description = "UpCloud plan slug (e.g. 1xCPU-1GB)."
  type        = string
  default     = "1xCPU-1GB"
}

variable "location" {
  description = "UpCloud zone (e.g. fi-hel1)."
  type        = string
  default     = "fi-hel1"
}

variable "image" {
  description = "UpCloud storage template title (e.g. Ubuntu Server 24.04 LTS (Noble Numbat))."
  type        = string
  default     = "Ubuntu Server 24.04 LTS (Noble Numbat)"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "ssh_user" {
  description = "Login user the operator key is attached to."
  type        = string
  default     = "root"
}

variable "disk_size" {
  description = "Root disk size in GB."
  type        = number
  default     = 25
}

variable "labels" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# UpCloud's firewall is stateful; only inbound rules are needed. The rule table
# is intentionally small: Tailscale direct path (UDP 41641) and ICMP.
variable "inbound_rules" {
  type = list(object({
    protocol  = string
    port_min  = optional(number)
    port_max  = optional(number)
    icmp_type = optional(string)
    family    = optional(string, "IPv4")
    comment   = optional(string)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "tcp", port_min = 22, port_max = 22, comment = "SSH bootstrap; closed by the host firewall after site.yml" },
    { protocol = "udp", port_min = 41641, port_max = 41641, comment = "Tailscale direct path" },
    { protocol = "icmp", icmp_type = "8", comment = "ICMP and PMTU" },
  ]
}
