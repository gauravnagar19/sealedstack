variable "dokploy_endpoint" {
  description = "Dokploy panel URL over the mesh, e.g. http://server-1:3000."
  type        = string
}

variable "dokploy_api_key" {
  description = "Dokploy API key (created through Dokploy's API on first boot and kept in the encrypted state; DOKPLOY_API_KEY in the secret source overrides it)."
  type        = string
  sensitive   = true
}

variable "registry_pull_token" {
  description = "Read-only pull credential for the selected registry (infra.REGISTRY_PULL_TOKEN, e.g. a ghcr PAT with read:packages). Empty disables the Dokploy registry registration."
  type        = string
  default     = ""
  sensitive   = true
}

variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (infra.state_passphrase). Non-sensitive on purpose: OpenTofu 1.9+ rejects sensitive variables in the encryption block. Provide it via TF_VAR_state_passphrase; a dummy value is fine for `init -backend=false`/validate."
  type        = string
  default     = ""
}

variable "server_ids" {
  description = "Dokploy remote-server ids keyed by server name. Leave empty for the control server."
  type        = map(string)
  default     = {}
}

variable "admin_user" {
  description = "The admin user Dokploy uses to SSH into worker servers."
  type        = string
  default     = "admin"
}

variable "dokploy_ssh_public_key" {
  description = "Public half of the Dokploy->worker key (generated into the encrypted state)."
  type        = string
  default     = ""
}

variable "dokploy_ssh_private_key" {
  description = "Private half of the Dokploy->worker key (generated into the encrypted state)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token used to manage R2 buckets for needs.object_storage. Falls back to CLOUDFLARE_API_TOKEN in the environment."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account id for R2 buckets. Falls back to cloudflare.account_id in the rendered stack model."
  type        = string
  default     = ""
}

variable "object_storage_access_key_id" {
  description = "S3 access key id injected into apps that declare needs.object_storage. Falls back to backup_access_key_id."
  type        = string
  default     = ""
  sensitive   = true
}

variable "object_storage_secret_access_key" {
  description = "S3 secret access key injected into apps that declare needs.object_storage. Falls back to backup_secret_access_key."
  type        = string
  default     = ""
  sensitive   = true
}

variable "backup_access_key_id" {
  description = "S3/R2 access key id for the selected backup destination."
  type        = string
  default     = ""
  sensitive   = true
}

variable "backup_secret_access_key" {
  description = "S3/R2 secret access key for the selected backup destination."
  type        = string
  default     = ""
  sensitive   = true
}

variable "backup_secondary_access_key_id" {
  description = "Access key id for the optional second (3-2-1) backup destination."
  type        = string
  default     = ""
  sensitive   = true
}

variable "backup_secondary_secret_access_key" {
  description = "Secret access key for the optional second (3-2-1) backup destination."
  type        = string
  default     = ""
  sensitive   = true
}
