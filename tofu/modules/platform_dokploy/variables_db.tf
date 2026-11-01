variable "database" {
  description = "Dokploy-managed Postgres on servers[0] (spec section 5.4, needs.postgres)."
  type = object({
    name              = string
    database          = string
    user              = string
    env               = string # env var that receives the connection string
    version           = string
    cron              = string
    keep              = number
    restore_on_create = bool
  })
  default = null
}

variable "external_database" {
  description = "External managed Postgres (needs.postgres.provider: neon). When set, no dokploy_postgres is created and this connection string is injected under `env`."
  type = object({
    env = string
    url = string
  })
  default   = null
  sensitive = true
}

variable "db_destination_id" {
  description = "Dokploy backup destination id (from the R2 bucket)."
  type        = string
  default     = ""
}

variable "db_backup_enabled" {
  description = "Whether a backup destination exists. A plain bool, known at plan time: the destination id is not known until the destination is created, and a count or for_each cannot depend on it."
  type        = bool
  default     = false
}

variable "secondary_backup_enabled" {
  description = "Whether the second (3-2-1) destination exists; known at plan time like db_backup_enabled."
  type        = bool
  default     = false
}

variable "db_destination_prefix" {
  description = "Backup object prefix, usually <app>/<db>/."
  type        = string
  default     = ""
}

variable "secondary_destination_id" {
  description = "A second, off-provider destination id (3-2-1). Empty disables the second copy."
  type        = string
  default     = ""
}

variable "db_secondary_prefix" {
  description = "Backup object prefix for the second destination."
  type        = string
  default     = ""
}

variable "volumes" {
  description = "Docker volumes to archive on a schedule (needs.volumes). `service` is the compose service that mounts the volume."
  type = list(object({
    volume  = string
    service = string
    cron    = string
    keep    = number
  }))
  default = []
}

variable "redis" {
  description = "Dokploy-managed Redis/Valkey on servers[0] (needs.redis)."
  type = object({
    name    = string
    version = string
    env     = string
  })
  default = null
}
