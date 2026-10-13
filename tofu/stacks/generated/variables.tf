variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (STATE_PASSPHRASE). Non-sensitive on purpose: OpenTofu rejects sensitive variables in the encryption block."
  type        = string
  default     = ""
}

variable "values" {
  description = "Values the engine learned at run time and must keep (the Dokploy API key, a 'shown once' flag). The engine reads the current map, merges its update and passes the whole map back, so nothing here is ever overwritten by an empty run."
  type        = map(string)
  default     = {}
  sensitive   = true
}
