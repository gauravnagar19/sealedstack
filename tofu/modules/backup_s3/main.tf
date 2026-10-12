# backup_s3 (spec section 4, registry backup.yaml): one module for every
# S3-compatible store — AWS S3, Backblaze B2, Wasabi, DigitalOcean Spaces,
# MinIO, Storj. The provider is configured by the stack from the slot config.

resource "aws_s3_bucket" "this" {
  count = var.create_bucket ? 1 : 0

  bucket = var.bucket

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  bucket = var.create_bucket ? aws_s3_bucket.this[0].bucket : var.bucket
}
