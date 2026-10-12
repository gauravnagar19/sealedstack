variable "app" {
  description = "App name; resources are named ss-<app> (D-013)."
  type        = string
}

variable "hostnames" {
  description = "Public hostnames routed to this app."
  type        = list(string)
}

variable "gated" {
  description = "Whether an OAuth check is enforced at the ngrok edge."
  type        = bool
  default     = false
}

variable "team_name" {
  description = "Identity hint recorded on ngrok resources (mirrors the cloudflare ingress contract)."
  type        = string
  default     = ""
}

variable "oauth_provider" {
  description = "ngrok OAuth provider for gated apps (google, github, microsoft, ...)."
  type        = string
  default     = "google"
}

variable "upstream" {
  description = "Upstream the connector forwards to (the tunnel target)."
  type        = string
  default     = "http://dokploy-traefik:80"
}

variable "internal_url" {
  description = "Internal ngrok endpoint the public cloud endpoint forwards to; bound by the connector. Defaults to http://ss-<app>.internal:80."
  type        = string
  default     = ""
}
