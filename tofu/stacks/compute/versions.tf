terraform {
  # Provider configurations with `for_each` require 1.9+; the S3 backend's
  # `use_lockfile` needs 1.10.
  required_version = ">= 1.10"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "1.70.0"
    }
    hostinger = {
      source  = "hostinger/hostinger"
      version = "0.1.23"
    }
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.43.0"
    }
    vultr = {
      source  = "vultr/vultr"
      version = "2.21.0"
    }
    linode = {
      source  = "linode/linode"
      version = "4.7.0"
    }
    scaleway = {
      source  = "scaleway/scaleway"
      version = "2.86.0"
    }
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    google = {
      source  = "hashicorp/google"
      version = "8.6.0"
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "5.9.0"
    }
    oci = {
      source  = "oracle/oci"
      version = "9.9.0"
    }
    upcloud = {
      source  = "UpCloudLtd/upcloud"
      version = "5.46.0"
    }
    ovh = {
      source  = "ovh/ovh"
      version = "2.22.0"
    }
    civo = {
      source  = "civo/civo"
      version = "1.3.3"
    }
  }
}
