variable "ssh_public_key" {
  description = "Operator public key installed on new servers."
  type        = string
  default     = ""
}

variable "state_passphrase" {
  description = "Passphrase for OpenTofu state encryption (infra.state_passphrase). Non-sensitive on purpose: OpenTofu 1.9+ rejects sensitive variables in the encryption block. Provide it via TF_VAR_state_passphrase; a dummy value is fine for `init -backend=false`/validate."
  type        = string
  default     = ""
}

variable "hcloud_token" {
  description = "Hetzner Cloud API token (infra.HCLOUD_TOKEN)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "hostinger_token" {
  description = "Hostinger API token (infra.HOSTINGER_TOKEN)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "digitalocean_token" {
  description = "DigitalOcean API token (infra.DIGITALOCEAN_TOKEN)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "vultr_api_key" {
  description = "Vultr API key (infra.VULTR_API_KEY)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "netcup_api_username" {
  description = "Netcup SCP username (infra.NETCUP_API_USERNAME)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "netcup_api_password" {
  description = "Netcup SCP password (infra.NETCUP_API_PASSWORD)."
  type        = string
  default     = ""
  sensitive   = true
}

# --- cloud providers (aws / gcp / azure) ------------------------------------

variable "aws_region" {
  description = "AWS region shared by all EC2 servers (infra.AWS_REGION)."
  type        = string
  default     = "eu-central-1"
}

variable "gcp_project" {
  description = "GCP project for Compute Engine (infra.GCP_PROJECT)."
  type        = string
  default     = ""
}

variable "gcp_region" {
  description = "GCP region; each server's `location` selects the zone."
  type        = string
  default     = "europe-west3"
}

variable "contabo_api_username" {
  description = "Contabo API client id (infra.CONTABO_CLIENT_ID)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "contabo_api_password" {
  description = "Contabo API client secret (infra.CONTABO_CLIENT_SECRET)."
  type        = string
  default     = ""
  sensitive   = true
}
