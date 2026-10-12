output "bucket" {
  value = azurerm_storage_container.this.name
}

output "endpoint" {
  description = "Blob service endpoint for the storage account."
  value       = data.azurerm_storage_account.this.primary_blob_endpoint
}
