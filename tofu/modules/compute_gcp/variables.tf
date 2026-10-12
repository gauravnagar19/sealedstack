variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  description = "Compute Engine machine type."
  type        = string
  default     = "e2-small"
}

variable "location" {
  description = "Compute Engine zone (e.g. europe-west3-a)."
  type        = string
  default     = "europe-west3-a"
}

variable "image" {
  description = "Boot image as project/family (e.g. ubuntu-os-cloud/ubuntu-2404-lts-amd64)."
  type        = string
  default     = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "ssh_user" {
  description = "Login user the operator key is attached to (metadata ssh-keys)."
  type        = string
  default     = "ubuntu"
}

variable "labels" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# Google Cloud VPC firewalls are stateful, so only inbound rules are needed:
# replies to outbound traffic are allowed automatically. The VPC's implied
# egress-allow rule is also made explicit below.
variable "inbound_rules" {
  type = list(object({
    protocol = string
    ports    = optional(list(string))
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { protocol = "tcp", ports = ["22"] },
    { protocol = "udp", ports = ["41641"] },
    { protocol = "icmp" },
  ]
}
