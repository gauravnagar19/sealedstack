variable "container" {
  description = "Name of the backups blob container (registry backup.yaml: azure_blob)."
  type        = string
}

variable "storage_account" {
  description = "Name of the existing storage account that holds the container."
  type        = string
}

variable "resource_group" {
  description = "Resource group of the storage account."
  type        = string
}

variable "container_access_type" {
  description = "Blob container access level: private, blob or container."
  type        = string
  default     = "private"
}
