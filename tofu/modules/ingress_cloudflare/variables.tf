variable "account_id" {
  type = string
}

variable "zone_id" {
  type = string
}

variable "app" {
  description = "App name; the tunnel is named ss-<app> (D-013)."
  type        = string
}

variable "hostnames" {
  description = "Public hostnames routed to this app."
  type        = list(string)
}

variable "gated" {
  description = "Whether Cloudflare Access is in front of this app."
  type        = bool
  default     = false
}

variable "team_name" {
  description = "Zero Trust team name, used for originRequest.access."
  type        = string
  default     = ""
}

variable "aud_by_hostname" {
  description = "Access application audience tags, keyed by hostname (for gated hostnames)."
  type        = map(string)
  default     = {}
}
