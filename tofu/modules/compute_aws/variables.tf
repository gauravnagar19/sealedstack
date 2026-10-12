variable "name" {
  description = "Server name (also its MagicDNS hostname)."
  type        = string
}

variable "server_type" {
  description = "EC2 instance type."
  type        = string
  default     = "t3.small"
}

variable "location" {
  description = "Availability zone (e.g. eu-central-1a). Empty lets EC2 choose; the provider region is configured on the stack's aws provider block."
  type        = string
  default     = ""
}

variable "image" {
  description = "AMI id (ami-...) or an AMI name glob resolved with data.aws_ami (Canonical owner)."
  type        = string
  # TODO: the owner below is Canonical; set a custom owner/ami id for other distros.
  default = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
}

variable "ssh_public_key" {
  description = "Operator public key installed at creation."
  type        = string
}

variable "tags" {
  type    = map(string)
  default = { managed-by = "sealedstack" }
}

# Security groups are stateful, so only inbound rules are needed: replies to
# outbound traffic are allowed automatically. This differs from the stateless
# rule table in spec section 7.4.
variable "inbound_rules" {
  type = list(object({
    ip_protocol = string
    from_port   = optional(number, -1)
    to_port     = optional(number, -1)
    cidr_ipv4   = optional(string, "0.0.0.0/0")
    description = optional(string)
  }))
  default = [
    # Temporary during bootstrap; the host firewall closes 22 on the WAN
    # interface after site.yml (see the module comment).
    { ip_protocol = "tcp", from_port = 22, to_port = 22, description = "SSH bootstrap; closed by the host firewall after site.yml" },
    { ip_protocol = "udp", from_port = 41641, to_port = 41641, description = "Tailscale direct path" },
    { ip_protocol = "icmp", description = "ICMP and PMTU" },
  ]
}
