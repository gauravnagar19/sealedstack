output "bucket" {
  value = cloudflare_r2_bucket.this.name
}

output "endpoint" {
  description = "S3 endpoint for the R2 account."
  value       = "https://${var.account_id}.r2.cloudflarestorage.com"
}
