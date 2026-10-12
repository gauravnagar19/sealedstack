# secrets_gcp (registry secrets.yaml): one Secret Manager secret (plus one
# version) per key. Secret IDs may only contain [a-zA-Z0-9_-], so key names are
# sanitised (dots and slashes become dashes) while outputs stay keyed by the
# original names.

locals {
  secret_ids = {
    for key in var.keys :
    key => replace(join("-", compact([var.prefix, key])), "/[^a-zA-Z0-9_-]/", "-")
  }
}

resource "google_secret_manager_secret" "this" {
  for_each = toset(var.keys)

  project   = var.project != "" ? var.project : null
  secret_id = local.secret_ids[each.key]
  labels    = var.labels

  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "this" {
  for_each = toset(var.keys)

  secret      = google_secret_manager_secret.this[each.key].id
  secret_data = var.values[each.key]
}
