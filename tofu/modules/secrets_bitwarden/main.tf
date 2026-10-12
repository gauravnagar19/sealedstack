# secrets_bitwarden (spec section 4 and 6): write the secrets OpenTofu creates
# (tunnel tokens, R2 credentials) back into Bitwarden Secrets Manager.

data "bitwarden-secrets_projects" "this" {
  provider = bws
}

locals {
  project_id = one([
    for project in data.bitwarden-secrets_projects.this.projects : project.id
    if project.name == var.project_name
  ])
}

resource "bitwarden-secrets_secret" "this" {
  provider = bws
  for_each = toset(var.keys)

  project_id = local.project_id
  key        = each.key
  value      = var.values[each.key]
  note       = var.note
}
