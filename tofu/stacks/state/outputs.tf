output "bucket" {
  value = aws_s3_bucket.state.bucket
}

output "endpoint" {
  value = "https://${var.account_id}.r2.cloudflarestorage.com"
}
