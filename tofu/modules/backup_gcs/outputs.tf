output "bucket" {
  value = local.bucket
}

output "endpoint" {
  description = "S3-compatible (XML API) endpoint for Cloud Storage."
  value       = "https://storage.googleapis.com"
}
