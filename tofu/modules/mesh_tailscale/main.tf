# mesh_tailscale (spec section 8, D-014).
# The whole policy file is managed here; manual console edits are overwritten.

resource "tailscale_acl" "this" {
  acl                        = var.policy
  overwrite_existing_content = var.overwrite_existing_content
}
