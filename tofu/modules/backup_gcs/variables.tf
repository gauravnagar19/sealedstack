variable "bucket" {
  description = "Name of the backups bucket (registry backup.yaml: gcs)."
  type        = string
}

variable "project" {
  description = "GCP project id. Empty uses the provider default."
  type        = string
  default     = ""
}

variable "location" {
  description = "GCS location, e.g. US, EU or europe-west1."
  type        = string
  default     = "US"
}

variable "create_bucket" {
  description = "Create the bucket. Turn off for providers that pre-create it."
  type        = bool
  default     = true
}
