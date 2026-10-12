output "connector_tokens" {
  description = "Connector token per app (Cloudflare tunnel or ngrok agent), consumed by the platform stack."
  value       = local.ingress_connector_tokens
  sensitive   = true
}

output "ingress_type" {
  description = "The selected ingress implementation, so the platform can pick the connector image."
  value       = local.slots.ingress
}

output "tunnel_ids" {
  value = { for name, ingress in module.ingress : name => ingress.tunnel_id }
}

output "aud_by_hostname" {
  value = local.use_cloudflare_gate ? module.gate[0].aud_by_hostname : {}
}

output "backup" {
  description = "The selected backup destination, consumed by the platform stack."
  value = {
    bucket   = local.backup_bucket
    endpoint = local.backup_endpoint
    region   = local.backup_region
  }
}
