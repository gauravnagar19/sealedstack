variable "account_id" {
  description = "Cloudflare account id (from config/stack.yaml)."
  type        = string
}

variable "bucket" {
  description = "State bucket name."
  type        = string
  default     = "sealedstack-state"
}

variable "r2_access_key_id" {
  description = "R2 S3 access key id (from Secrets Manager)."
  type        = string
  sensitive   = true
}

variable "r2_secret_access_key" {
  description = "R2 S3 secret access key (from Secrets Manager)."
  type        = string
  sensitive   = true
}

variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (infra.state_passphrase). Non-sensitive on purpose: OpenTofu 1.9+ rejects sensitive variables in the encryption block. Provide it via TF_VAR_state_passphrase; a dummy value is fine for `init -backend=false`/validate."
  type        = string
  default     = ""
}
