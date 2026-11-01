# state stack (spec section 10, D-007): creates the encrypted state bucket.
# Run once with local state: `just state-init`.

terraform {
  # OpenTofu native state encryption: the state holds tunnel tokens and R2 keys.
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

provider "aws" {
  region     = "auto"
  access_key = var.r2_access_key_id
  secret_key = var.r2_secret_access_key

  endpoints {
    s3 = "https://${var.account_id}.r2.cloudflarestorage.com"
  }

  skip_credentials_validation = true
  skip_region_validation      = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true
}

resource "aws_s3_bucket" "state" {
  bucket = var.bucket

  lifecycle {
    # The state bucket holds every stack's encrypted state; never let a config
    # mistake destroy it silently.
    prevent_destroy = true
  }
}

# No bucket versioning: Cloudflare R2 does not implement PutBucketVersioning (HTTP 501, first live run), so asking
# for it made state-init fail after the bucket was already created. The state is encrypted (above) and the
# remote state lock protects it from concurrent writes; keep a copy of it with your own backup routine if you
# need history.
