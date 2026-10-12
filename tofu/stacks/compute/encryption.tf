# OpenTofu native state encryption (D-007). The state holds tunnel tokens and
# database credentials, so it is encrypted at rest with a passphrase from
# Secrets Manager (infra.state_passphrase).
terraform {
  encryption {
    key_provider "pbkdf2" "state" {
      passphrase = var.state_passphrase
    }

    method "aes_gcm" "state" {
      keys = key_provider.pbkdf2.state
    }

    state {
      method = method.aes_gcm.state
    }

    plan {
      method = method.aes_gcm.state
    }
  }
}
