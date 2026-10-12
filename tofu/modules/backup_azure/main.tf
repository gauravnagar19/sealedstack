# backup_azure (spec section 4, registry backup.yaml): the Azure Blob
# destination for database backups. The storage account is managed outside this
# module (the operator pre-creates it, as with the S3 family); this module owns
# the private container and reports the account's blob endpoint.

data "azurerm_storage_account" "this" {
  name                = var.storage_account
  resource_group_name = var.resource_group
}

resource "azurerm_storage_container" "this" {
  name                  = var.container
  storage_account_id    = data.azurerm_storage_account.this.id
  container_access_type = var.container_access_type

  lifecycle {
    prevent_destroy = true
  }
}
