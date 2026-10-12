variable "key_vault_id" {
  description = "Resource ID of the Key Vault to write secrets into."
  type        = string
}

variable "prefix" {
  description = "Prefix for secret names, e.g. sealedstack-infra (empty writes bare key names)."
  type        = string
  default     = ""
}

variable "keys" {
  description = "Secret names to write (non-sensitive, safe as for_each keys)."
  type        = list(string)
}

variable "values" {
  description = "Secret name -> value. Keyed by the same names as `keys`."
  type        = map(string)
  sensitive   = true
}

variable "content_type" {
  description = "Optional content type applied to every secret."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Tags applied to every secret."
  type        = map(string)
  default     = {}
}
