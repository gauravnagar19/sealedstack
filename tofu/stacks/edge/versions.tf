terraform {
  # Provider configurations with `for_each` require 1.9+; the S3 backend's
  # `use_lockfile` needs 1.10.
  required_version = ">= 1.10"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.27.0"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.2"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    zerotier = {
      source  = "zerotier/zerotier"
      version = "1.6.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "8.6.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.9.0"
    }
    ngrok = {
      source  = "ngrok/ngrok"
      version = "0.8.1"
    }
  }
}
