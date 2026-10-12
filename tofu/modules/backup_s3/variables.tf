variable "bucket" {
  description = "Name of the backups bucket."
  type        = string
}

variable "endpoint" {
  description = "S3-compatible endpoint, e.g. https://s3.eu-central-1.wasabisys.com."
  type        = string
}

variable "create_bucket" {
  description = "Create the bucket. Turn off for providers that pre-create it."
  type        = bool
  default     = true
}
