variable "account_id" {
  type = string
}

variable "bucket" {
  description = "Name of the backups bucket (spec section 8)."
  type        = string
}

variable "location" {
  description = "R2 location hint (e.g. ENAM, WEUR). Empty for automatic."
  type        = string
  default     = ""
}
