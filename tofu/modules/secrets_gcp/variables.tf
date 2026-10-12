variable "project" {
  description = "GCP project ID to write secrets into (empty uses the provider default)."
  type        = string
  default     = ""
}

variable "prefix" {
  description = "Prefix for secret IDs, e.g. sealedstack-infra (empty writes bare key names)."
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

variable "labels" {
  description = "Labels applied to every secret."
  type        = map(string)
  default     = {}
}
