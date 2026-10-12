variable "project_name" {
  description = "Secrets Manager project name (infra, prod or staging)."
  type        = string
}

variable "keys" {
  description = "Names of the secrets to create (non-sensitive, safe as for_each keys)."
  type        = list(string)
}

variable "values" {
  description = "Secret key -> value pairs. Keyed by the same names as `keys`."
  type        = map(string)
  sensitive   = true
}

variable "note" {
  description = "Note attached to every written secret."
  type        = string
  default     = "Managed by sealedstack."
}
