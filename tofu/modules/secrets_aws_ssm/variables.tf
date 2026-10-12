variable "prefix" {
  description = "Parameter name prefix, e.g. /sealedstack/infra."
  type        = string
}

variable "keys" {
  description = "Parameter names to write (non-sensitive, safe as for_each keys)."
  type        = list(string)
}

variable "values" {
  description = "Parameter name -> value."
  type        = map(string)
  sensitive   = true
}
