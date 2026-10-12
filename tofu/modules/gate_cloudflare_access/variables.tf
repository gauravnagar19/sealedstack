variable "account_id" {
  type = string
}

variable "zone_id" {
  type = string
}

variable "domain" {
  description = "Apex domain, for the catch-all Access app."
  type        = string
}

variable "policies" {
  description = "Reusable gate policies from stack.yaml (spec section 5.1)."
  type = map(object({
    emails  = list(string)
    session = string
  }))
}

variable "gated" {
  description = "Gated hostnames mapped to the policy name that protects them."
  type        = map(string)
  default     = {}
}

variable "catch_all" {
  description = "Add a deny-by-default Access app on *.<domain>."
  type        = bool
  default     = false
}

variable "bypass" {
  description = "Hostnames mapped to webhook paths that must bypass the gate."
  type        = map(list(string))
  default     = {}
}

variable "public_hostnames" {
  description = "Public hostnames covered by the catch-all that must be allowed through (H4)."
  type        = list(string)
  default     = []
}
