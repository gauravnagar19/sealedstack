variable "name" {
  description = "App name (project and compose name)."
  type        = string
}

variable "servers" {
  description = "Servers this app runs on. servers[0] is the database primary."
  type        = list(string)
}

variable "server_ids" {
  description = "Dokploy remote-server ids, keyed by server name. Omit the control server."
  type        = map(string)
  default     = {}
}

variable "hostnames" {
  description = "Public hostnames to route to the web service."
  type        = list(string)
}

variable "web_service" {
  type = string
}

variable "web_port" {
  type = number
}

variable "source_type" {
  description = "image or repo."
  type        = string
  validation {
    condition     = contains(["image", "repo"], var.source_type)
    error_message = "source_type must be image or repo."
  }
}

variable "build_mode" {
  description = "ci (default, D-030): Dokploy pulls the rendered deploy compose; server: Dokploy clones at ref and builds."
  type        = string
  default     = "ci"
  validation {
    condition     = contains(["ci", "server"], var.build_mode)
    error_message = "build_mode must be ci or server."
  }
}

variable "deploy_compose" {
  description = "Rendered deploy compose (build: stripped) for build_mode ci, from .build/apps/<app>.deploy-compose.yml."
  type        = string
  default     = ""
}

variable "image" {
  type    = string
  default = ""
}

variable "repo" {
  type    = string
  default = ""
}

variable "ref" {
  type    = string
  default = ""
}

variable "compose_path" {
  type    = string
  default = "docker-compose.yml"
}

variable "environment_variables" {
  description = "Secrets and connection strings injected as env vars (spec section 6)."
  type        = map(string)
  default     = {}
  sensitive   = true
}

variable "connector_image" {
  description = "Pinned cloudflared image (D-013)."
  type        = string
  default     = "cloudflare/cloudflared:2024.10.0"
}

variable "connector_type" {
  description = "Ingress implementation driving the connector: cloudflare or ngrok."
  type        = string
  default     = "cloudflare"
}

variable "ngrok_connector_image" {
  description = "Pinned ngrok agent image, used when connector_type is ngrok."
  type        = string
  default     = "ngrok/ngrok:3.16.0"
}

