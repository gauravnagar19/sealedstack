variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_id" {
  description = "Provider-native server identifier (Netcup SCP server name, e.g. v2202...)."
  type        = string
}

variable "spec_path" {
  description = "Path to config/providers/<provider>.yaml describing the API."
  type        = string
}

variable "enabled" {
  description = "Apply the provider firewall through the API. Off by default: the operator applies rules by hand."
  type        = bool
  default     = false
}

variable "firewall_rules" {
  description = "The rule table from spec section 7.4."
  type = list(object({
    direction = string
    protocol  = string
    src_port  = optional(string)
    dst_port  = optional(string)
    action    = optional(string, "ACCEPT")
    note      = optional(string)
  }))
  default = []
}

variable "api_username" {
  type      = string
  default   = ""
  sensitive = true
}

variable "api_password" {
  type      = string
  default   = ""
  sensitive = true
}

variable "api_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "scripts_dir" {
  description = "Absolute path to the repository scripts/ directory."
  type        = string
}
