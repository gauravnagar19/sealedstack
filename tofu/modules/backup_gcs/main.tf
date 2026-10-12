# backup_gcs (spec section 4, registry backup.yaml): the GCS destination for
# database backups. A dedicated Cloud Storage bucket, private and encrypted by
# default. The S3-compatible XML endpoint is reported so the platform's backup
# job can use the same S3 client as the other backup slots.

resource "google_storage_bucket" "this" {
  count = var.create_bucket ? 1 : 0

  name                        = var.bucket
  location                    = var.location
  project                     = var.project != "" ? var.project : null
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  bucket = var.create_bucket ? google_storage_bucket.this[0].name : var.bucket
}
