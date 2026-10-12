terraform {
  # Provider configurations with `for_each` require 1.9+; the S3 backend's
  # `use_lockfile` needs 1.10.
  required_version = ">= 1.10"

  required_providers {
    dokploy = {
      source  = "vanillauys/dokploy"
      version = "1.9.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.27.0"
    }
  }
}
