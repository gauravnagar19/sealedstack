variable "cloudflare_api_token" {
  description = "Cloudflare API token (infra.CLOUDFLARE_API_TOKEN)."
  type        = string
  sensitive   = true
}

variable "tailscale_oauth_client_id" {
  description = "Tailscale OAuth client id (infra.TAILSCALE_OAUTH_CLIENT_ID)."
  type        = string
  sensitive   = true
}

variable "tailscale_oauth_client_secret" {
  description = "Tailscale OAuth client secret (infra.TAILSCALE_OAUTH_CLIENT_SECRET)."
  type        = string
  sensitive   = true
}

variable "bws_access_token" {
  description = "Bitwarden machine-account token (BWS_ACCESS_TOKEN)."
  type        = string
  sensitive   = true
}

variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (infra.state_passphrase). Non-sensitive on purpose: OpenTofu 1.9+ rejects sensitive variables in the encryption block. Provide it via TF_VAR_state_passphrase; a dummy value is fine for `init -backend=false`/validate."
  type        = string
  default     = ""
}

# --- backup slot ------------------------------------------------------------

variable "backup_endpoint" {
  description = "S3 endpoint for the chosen backup provider (s3-family only)."
  type        = string
  default     = ""
}

variable "backup_region" {
  type    = string
  default = "auto"
}

variable "backup_location" {
  description = "GCS bucket location (backup slot: gcs)."
  type        = string
  default     = "US"
}

variable "azure_storage_account" {
  description = "Azure storage account (backup slot: azure_blob)."
  type        = string
  default     = ""
}

variable "azure_resource_group" {
  description = "Azure resource group (backup slot: azure_blob)."
  type        = string
  default     = ""
}

variable "backup_access_key_id" {
  type      = string
  default   = ""
  sensitive = true
}

variable "backup_secret_access_key" {
  type      = string
  default   = ""
  sensitive = true
}

# --- secrets slot (aws_ssm) -------------------------------------------------

variable "ssm_prefix" {
  description = "AWS SSM parameter prefix."
  type        = string
  default     = "/sealedstack/infra"
}

variable "ssm_region" {
  type    = string
  default = "eu-central-1"
}

variable "ssm_access_key_id" {
  type      = string
  default   = ""
  sensitive = true
}

variable "ssm_secret_access_key" {
  type      = string
  default   = ""
  sensitive = true
}

# --- mesh slot (zerotier) ---------------------------------------------------

variable "zerotier_api_token" {
  description = "ZeroTier Central API token (infra.ZEROTIER_API_TOKEN)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "zerotier_members" {
  description = "Server name -> ZeroTier node address, reported by the client after it joins."
  type        = map(string)
  default     = {}
}

# --- secrets slot (gcp / azure_kv / openbao / vault) ------------------------

variable "gcp_project" {
  type    = string
  default = ""
}

variable "gcp_secret_prefix" {
  type    = string
  default = "sealedstack-infra"
}

variable "azure_subscription_id" {
  description = "Azure subscription id for the azurerm provider (secrets slot: azure_kv, backup slot: azure_blob). Falls back to ARM_SUBSCRIPTION_ID in the environment; a placeholder is used when unset so an unselected slot does not fail plan."
  type        = string
  default     = ""
}

variable "azure_key_vault_id" {
  type    = string
  default = ""
}

variable "azure_secret_prefix" {
  type    = string
  default = "sealedstack-infra"
}

variable "openbao_addr" {
  type    = string
  default = ""
}

variable "openbao_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "openbao_mount" {
  type    = string
  default = "secret"
}

variable "openbao_prefix" {
  type    = string
  default = "sealedstack/infra"
}

variable "vault_addr" {
  type    = string
  default = ""
}

variable "vault_token" {
  type      = string
  default   = ""
  sensitive = true
}

variable "vault_mount" {
  type    = string
  default = "secret"
}

variable "vault_prefix" {
  type    = string
  default = "sealedstack/infra"
}

# --- ingress slot (ngrok) ---------------------------------------------------

variable "ngrok_api_key" {
  description = "ngrok API key (infra.NGROK_API_KEY)."
  type        = string
  default     = ""
  sensitive   = true
}
