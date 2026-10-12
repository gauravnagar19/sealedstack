variable "policy" {
  description = "The entire Tailscale ACL policy file (HuJSON). OpenTofu owns the whole file (D-014)."
  type        = string
}

variable "overwrite_existing_content" {
  description = "Overwrite any manual edits in the Tailscale console."
  type        = bool
  default     = true
}
