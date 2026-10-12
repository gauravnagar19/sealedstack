# secrets_vault (registry secrets.yaml): one KV v2 secret per key. Each secret
# stores { "value": <secret> } so the read path stays name -> value.
#
# `vault_kv_secret_v2` builds the full path as <mount>/data/<name>; `name` is
# left as the bare key (dots are valid in Vault paths).

locals {
  paths = {
    for key in var.keys :
    key => join("/", compact([trim(var.prefix, "/"), key]))
  }
}

resource "vault_kv_secret_v2" "this" {
  for_each = toset(var.keys)

  mount     = var.mount
  name      = local.paths[each.key]
  data_json = jsonencode({ value = var.values[each.key] })
}
