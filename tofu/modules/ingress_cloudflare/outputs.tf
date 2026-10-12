output "tunnel_id" {
  value = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

output "tunnel_token" {
  description = "Connector token; written to infra.tunnel.<app> in Secrets Manager."
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.this.token
  sensitive   = true
}

output "hostnames" {
  value = var.hostnames
}
