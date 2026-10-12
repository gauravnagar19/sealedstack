variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "ip" {
  description = "Public IP supplied by the operator, if any."
  type        = string
  default     = ""
}
