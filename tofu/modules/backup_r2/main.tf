# backup_r2 (spec section 8): the S3-compatible destination for database
# backups. Scoped R2 S3 credentials are created outside OpenTofu (Q1) and stored
# in Secrets Manager; this module owns the bucket.

resource "cloudflare_r2_bucket" "this" {
  account_id = var.account_id
  name       = var.bucket
  location   = var.location != "" ? var.location : null

  lifecycle {
    prevent_destroy = true
  }
}
