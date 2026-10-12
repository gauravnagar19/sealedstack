variable "mount" {
  description = "KV v2 secrets engine mount path, e.g. secret."
  type        = string
}

variable "prefix" {
  description = "Path prefix under the mount, e.g. sealedstack/infra (empty writes bare key names)."
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
