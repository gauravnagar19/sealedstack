variable "name" {
  description = "Name of the managed ZeroTier network."
  type        = string
  default     = "sealedstack"
}

variable "description" {
  description = "Human description of the network."
  type        = string
  default     = "sealedstack mesh"
}

variable "private" {
  description = "Private networks require each member to be authorized (recommended)."
  type        = bool
  default     = true
}

variable "assign_ipv4" {
  description = "Let ZeroTier assign IPv4 addresses to members."
  type        = bool
  default     = true
}

variable "dns_servers" {
  description = "DNS servers advertised to members; the dns block is omitted when empty."
  type        = list(string)
  default     = []
}

variable "dns_domain" {
  description = "Search domain for the network DNS block (used only when dns_servers is set)."
  type        = string
  default     = "sealedstack.internal"
}

variable "servers" {
  description = <<-EOT
    Servers to join to the network, keyed by server name (the mesh contract's
    private hostname). Each value carries the ZeroTier node address the client
    reports after `zerotier-cli info` (`member_id`), plus optional overrides.
  EOT
  type = map(object({
    member_id          = string
    authorized         = optional(bool, true)
    description        = optional(string)
    hidden             = optional(bool, false)
    no_auto_assign_ips = optional(bool, false)
  }))
  default = {}
}
