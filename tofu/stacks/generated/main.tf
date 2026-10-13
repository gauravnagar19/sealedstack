# generated stack (D-038, D-044, M7-complete Task 7): every value sealedstack
# generates rather than the user providing lives here, in the encrypted state,
# never in the secret source and never in git.
#
#   - the Dokploy admin password (random),
#   - the Dokploy -> worker SSH key pair (used by the platform stack),
#   - the CI SSH key pair (authorised for the admin user at bootstrap, so a CI
#     runner on the tailnet can run the baseline over SSH),
#   - a small map of run-time values (the Dokploy API key created through
#     Dokploy's API on first boot).
#
# Nothing here talks to a provider; it only generates and stores.

resource "random_password" "dokploy_admin" {
  length  = 32
  special = false
}

resource "tls_private_key" "dokploy_worker" {
  algorithm = "ED25519"
}

resource "tls_private_key" "ci" {
  algorithm = "ED25519"
}

# The run-time values. `values` is the complete map every time (the engine merges),
# so an update is a plain in-place change.
resource "terraform_data" "values" {
  input = var.values
}
