# secrets_azure_kv (registry secrets.yaml): one Key Vault secret per key.
# Key Vault names may only contain [0-9a-zA-Z-] and are capped at 127 chars, so
# names are sanitised (dots and slashes become dashes) while outputs stay keyed
# by the original names.

locals {
  secret_names = {
    for key in var.keys :
    key => substr(
      replace(join("-", compact([var.prefix, key])), "/[^0-9a-zA-Z-]/", "-"),
      0, 127,
    )
  }
}

resource "azurerm_key_vault_secret" "this" {
  for_each = toset(var.keys)

  name         = local.secret_names[each.key]
  value        = var.values[each.key]
  key_vault_id = var.key_vault_id
  content_type = var.content_type != "" ? var.content_type : null
  tags         = var.tags
}
