# compute_netcup (spec section 4): there is no supported OpenTofu provider, so
# firewall changes are made through the SCP REST API. The exact request shape is
# described by config/providers/netcup.yaml so it can be filled in from the SCP
# API documentation without touching code. When `enabled` is false the operator
# applies the rule table by hand (the any-ssh behaviour).
#
# See docs/providers.md for the spec file format.

resource "terraform_data" "firewall" {
  count = var.enabled ? 1 : 0

  triggers_replace = {
    rules     = jsonencode(var.firewall_rules)
    spec      = var.enabled ? filemd5(var.spec_path) : ""
    server_id = var.server_id
  }

  provisioner "local-exec" {
    command = "python3 -m sealedstack.firewall"

    environment = {
      PROVIDER     = "netcup"
      SERVER_NAME  = var.name
      SERVER_ID    = var.server_id
      SPEC_PATH    = var.spec_path
      RULES_JSON   = jsonencode(var.firewall_rules)
      API_USERNAME = var.api_username
      API_PASSWORD = var.api_password
      API_TOKEN    = var.api_token
    }
  }
}
