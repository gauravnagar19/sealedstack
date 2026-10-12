output "connector_token" {
  description = "ngrok agent authtoken; written to infra.tunnel.<app> in Secrets Manager."
  value       = ngrok_credential.this.token
  sensitive   = true
}

output "domains" {
  description = "Reserved domains, keyed by hostname."
  value       = { for hostname, domain in ngrok_domain.this : hostname => domain.domain }
}

output "endpoints" {
  description = "Public cloud endpoint URLs, keyed by hostname."
  value       = { for hostname, endpoint in ngrok_cloud_endpoint.this : hostname => endpoint.url }
}

output "hostnames" {
  value = var.hostnames
}

output "agent_config" {
  description = "ngrok agent configuration (internal endpoint + upstream) for the connector."
  value = yamlencode({
    version = "3"
    agent   = { authtoken = ngrok_credential.this.token }
    endpoints = [
      {
        name     = "ss-${var.app}"
        url      = local.internal_url
        upstream = { url = var.upstream }
      }
    ]
  })
  sensitive = true
}
