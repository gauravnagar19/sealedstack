output "aud_by_hostname" {
  description = "Access audience tags keyed by hostname (consumed by ingress_cloudflare)."
  value       = { for hostname, app in cloudflare_zero_trust_access_application.gated : hostname => app.aud }
}
