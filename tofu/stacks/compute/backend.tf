# Encrypted R2 state backend (D-007). Fully partial: the bucket, endpoint,
# credentials and state key are supplied at `init` by tofu_ready, so the same
# stacks can run against separate e2e state (N8).
terraform {
  backend "s3" {}
}
