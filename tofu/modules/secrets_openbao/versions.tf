# OpenBao publishes no dedicated OpenTofu/Terraform provider (verified:
# registry.opentofu.org/openbao/openbao and registry.terraform.io/openbao/openbao
# both 404). OpenBao is API-compatible with Vault, so this module uses the
# hashicorp/vault provider pointed at the OpenBao address.
terraform {
  required_providers {
    vault = {
      source = "hashicorp/vault"
    }
  }
}
