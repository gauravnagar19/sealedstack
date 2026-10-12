# compute stack (spec section 4, v0.4): provision or manage provider firewalls,
# dispatching per server by its `provider` in config/servers.yaml.
#
#   hetzner     -> hcloud server + stateful firewall (created here)
#   digitalocean-> droplet + cloud firewall
#   vultr       -> instance + firewall group
#   linode      -> Linode instance + cloud firewall
#   scaleway    -> Scaleway instance + security group
#   hostinger   -> native provider VPS (no provider firewall; UFW protects)
#   netcup      -> SCP REST API adapter (config/providers/netcup.yaml)
#   any-ssh     -> nothing; the operator applies the rule table by hand

# Every compute provider is declared as an *aliased* configuration with
# `for_each`: when no server uses it the map is empty, so the provider has zero
# instances and is never configured (no inert placeholder values). Each module
# selects the single active instance as `<provider>.this["default"]`.
locals {
  on  = { default = true }
  off = {}
}

provider "hcloud" {
  alias    = "this"
  for_each = local.use_hcloud ? local.on : local.off

  token = var.hcloud_token
}

provider "hostinger" {
  alias    = "this"
  for_each = local.use_hostinger ? local.on : local.off

  api_token = var.hostinger_token
}

provider "digitalocean" {
  alias    = "this"
  for_each = local.use_digitalocean ? local.on : local.off

  token = var.digitalocean_token
}

provider "vultr" {
  alias    = "this"
  for_each = local.use_vultr ? local.on : local.off

  api_key = var.vultr_api_key
}

# Credentials come from the environment (LINODE_TOKEN).
provider "linode" {
  alias    = "this"
  for_each = contains(local.server_providers, "linode") ? local.on : local.off
}

# Credentials come from the environment (SCW_ACCESS_KEY / SCW_SECRET_KEY).
provider "scaleway" {
  alias    = "this"
  for_each = contains(local.server_providers, "scaleway") ? local.on : local.off
}

provider "aws" {
  alias    = "this"
  for_each = local.use_aws ? local.on : local.off

  region = var.aws_region != "" ? var.aws_region : "us-east-1"
}

provider "google" {
  alias    = "this"
  for_each = local.use_gcp ? local.on : local.off

  project = var.gcp_project
  region  = var.gcp_region != "" ? var.gcp_region : "us-central1"
}

provider "azurerm" {
  alias    = "this"
  for_each = local.use_azure ? local.on : local.off

  features {}
}

# More clouds: credentials from the environment (OCI_* / UPCLOUD_* / OVH_* / CIVO_TOKEN).
provider "oci" {
  alias    = "this"
  for_each = local.use_oracle ? local.on : local.off
}

provider "upcloud" {
  alias    = "this"
  for_each = local.use_upcloud ? local.on : local.off
}

provider "ovh" {
  alias    = "this"
  for_each = local.use_ovh ? local.on : local.off
}

# TODO(civo): the civo provider's `token` argument is deprecated; if it is set in
# this block the provider emits a "Deprecated Attribute Usage" runtime warning.
# sealedstack deliberately sets no arguments here and authenticates with the
# CIVO_TOKEN environment variable instead (registry/compute.yaml `secrets`), so
# no warning is produced. Revisit when the provider removes the argument and
# `api_endpoint`/env-only auth becomes the sole supported form.
provider "civo" {
  alias    = "this"
  for_each = local.use_civo ? local.on : local.off
}

locals {
  build_dir = "${path.root}/../../../.build"
  servers   = jsondecode(file("${local.build_dir}/servers.json")).servers

  # `slots.compute` is the default compute implementation (spec §4). Servers still
  # name their own provider; this is read so the dispatch is explicit and the
  # any-ssh implementation is wired below.
  stack        = jsondecode(file("${local.build_dir}/stack.json"))
  compute_impl = try(local.stack.slots.compute, "")

  # Providers actually referenced by config/servers.yaml; the rest stay inert.
  server_providers = toset([for _name, spec in local.servers : spec.provider])
  use_hcloud       = contains(local.server_providers, "hetzner")
  use_hostinger    = contains(local.server_providers, "hostinger")
  use_digitalocean = contains(local.server_providers, "digitalocean")
  use_vultr        = contains(local.server_providers, "vultr")
  use_aws          = contains(local.server_providers, "aws")
  use_gcp          = contains(local.server_providers, "gcp")
  use_azure        = contains(local.server_providers, "azure")
  use_oracle       = contains(local.server_providers, "oracle")
  use_upcloud      = contains(local.server_providers, "upcloud")
  use_ovh          = contains(local.server_providers, "ovh")
  use_civo         = contains(local.server_providers, "civo")

  netcup_spec  = "${path.root}/../../../config/providers/netcup.yaml"
  contabo_spec = "${path.root}/../../../config/providers/contabo.yaml"

  # The stateless rule table from spec section 7.4, passed to the REST adapter.
  netcup_rules = [
    { direction = "in", protocol = "tcp", dst_port = "22", note = "SSH bootstrap; remove after site.yml closes 22 (docs/netcup-firewall.md)" },
    { direction = "in", protocol = "udp", src_port = "53", note = "DNS replies" },
    { direction = "in", protocol = "udp", src_port = "123", note = "NTP replies" },
    { direction = "in", protocol = "udp", src_port = "7844", note = "cloudflared QUIC replies" },
    { direction = "in", protocol = "udp", dst_port = "41641", note = "Tailscale direct path" },
    { direction = "in", protocol = "icmp", note = "PMTU and IPv6 ND" },
    { direction = "in", protocol = "tcp", action = "DROP" },
    { direction = "in", protocol = "udp", action = "DROP" },
    { direction = "out", protocol = "tcp", dst_port = "25", action = "DROP" },
    { direction = "out", protocol = "tcp", dst_port = "465", action = "DROP" },
    { direction = "out", protocol = "tcp", dst_port = "587", action = "DROP" },
  ]
}

module "hetzner" {
  source   = "../../modules/compute_hetzner"
  for_each = { for name, s in local.servers : name => s if s.provider == "hetzner" }

  providers = {
    hcloud = hcloud.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "cx22")
  image          = try(each.value.os, "ubuntu-24.04")
  location       = try(each.value.location, "fsn1")
  ssh_public_key = var.ssh_public_key
}

module "hostinger" {
  source   = "../../modules/compute_hostinger"
  for_each = { for name, s in local.servers : name => s if s.provider == "hostinger" }

  providers = {
    hostinger = hostinger.this["default"]
  }

  name           = each.key
  plan           = each.value.plan
  template_id    = each.value.template_id
  data_center_id = each.value.data_center_id
  ssh_public_key = var.ssh_public_key
}

module "digitalocean" {
  source   = "../../modules/compute_digitalocean"
  for_each = { for name, s in local.servers : name => s if s.provider == "digitalocean" }

  providers = {
    digitalocean = digitalocean.this["default"]
  }

  name           = each.key
  size           = try(each.value.server_type, "s-1vcpu-2gb")
  region         = try(each.value.location, "fra1")
  image          = try(each.value.image, each.value.os == "debian-12" ? "debian-12-x64" : "ubuntu-24-04-x64")
  ssh_public_key = var.ssh_public_key
}

module "vultr" {
  source   = "../../modules/compute_vultr"
  for_each = { for name, s in local.servers : name => s if s.provider == "vultr" }

  providers = {
    vultr = vultr.this["default"]
  }

  name           = each.key
  plan           = try(each.value.server_type, "vc2-1c-2gb")
  region         = try(each.value.location, "fra")
  os_id          = try(tonumber(each.value.os_id), 2284)
  ssh_public_key = var.ssh_public_key
}

module "linode" {
  source   = "../../modules/compute_linode"
  for_each = { for name, s in local.servers : name => s if s.provider == "linode" }

  providers = {
    linode = linode.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "g6-nanode-1")
  location       = try(each.value.location, "eu-central")
  image          = try(each.value.image, each.value.os == "debian-12" ? "linode/debian12" : "linode/ubuntu24.04")
  ssh_public_key = var.ssh_public_key
}

module "scaleway" {
  source   = "../../modules/compute_scaleway"
  for_each = { for name, s in local.servers : name => s if s.provider == "scaleway" }

  providers = {
    scaleway = scaleway.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "DEV1-S")
  location       = try(each.value.location, "fr-par-1")
  image          = try(each.value.image, each.value.os == "debian-12" ? "debian_bookworm" : "ubuntu_noble")
  ssh_public_key = var.ssh_public_key
}

module "aws" {
  source   = "../../modules/compute_aws"
  for_each = { for name, s in local.servers : name => s if s.provider == "aws" }

  providers = {
    aws = aws.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "t3.small")
  location       = try(each.value.location, "")
  image          = try(each.value.image, "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*")
  ssh_public_key = var.ssh_public_key
}

module "gcp" {
  source   = "../../modules/compute_gcp"
  for_each = { for name, s in local.servers : name => s if s.provider == "gcp" }

  providers = {
    google = google.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "e2-small")
  location       = try(each.value.location, "europe-west3-a")
  image          = try(each.value.image, "ubuntu-os-cloud/ubuntu-2404-lts-amd64")
  ssh_public_key = var.ssh_public_key
}

module "azure" {
  source   = "../../modules/compute_azure"
  for_each = { for name, s in local.servers : name => s if s.provider == "azure" }

  providers = {
    azurerm = azurerm.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "Standard_B1s")
  location       = try(each.value.location, "westeurope")
  image          = try(each.value.image, "Canonical:ubuntu-24_04-lts:server:latest")
  ssh_public_key = var.ssh_public_key
}

module "oracle" {
  source   = "../../modules/compute_oracle"
  for_each = { for name, s in local.servers : name => s if s.provider == "oracle" }

  providers = {
    oci = oci.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "VM.Standard.A1.Flex")
  location       = try(each.value.location, "")
  image          = try(each.value.image, "Canonical-Ubuntu-24.04")
  compartment_id = try(each.value.compartment_id, "")
  ssh_public_key = var.ssh_public_key
}

module "upcloud" {
  source   = "../../modules/compute_upcloud"
  for_each = { for name, s in local.servers : name => s if s.provider == "upcloud" }

  providers = {
    upcloud = upcloud.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "1xCPU-1GB")
  location       = try(each.value.location, "fi-hel1")
  image          = try(each.value.image, "Ubuntu Server 24.04 LTS (Noble Numbat)")
  ssh_public_key = var.ssh_public_key
}

module "ovh" {
  source   = "../../modules/compute_ovh"
  for_each = { for name, s in local.servers : name => s if s.provider == "ovh" }

  providers = {
    ovh = ovh.this["default"]
  }

  name           = each.key
  service_name   = try(each.value.service_name, "")
  server_type    = try(each.value.server_type, "b3-8")
  location       = try(each.value.location, "GRA11")
  image          = try(each.value.image, "Ubuntu 24.04")
  ssh_public_key = var.ssh_public_key
}

module "civo" {
  source   = "../../modules/compute_civo"
  for_each = { for name, s in local.servers : name => s if s.provider == "civo" }

  providers = {
    civo = civo.this["default"]
  }

  name           = each.key
  server_type    = try(each.value.server_type, "g3.xsmall")
  location       = try(each.value.location, "LON1")
  image          = try(each.value.image, "ubuntu-noble")
  ssh_public_key = var.ssh_public_key
}

module "netcup" {
  source   = "../../modules/compute_netcup"
  for_each = { for name, s in local.servers : name => s if s.provider == "netcup" }

  name           = each.key
  server_id      = try(each.value.external_id, each.key)
  spec_path      = local.netcup_spec
  enabled        = fileexists(local.netcup_spec)
  firewall_rules = local.netcup_rules
  api_username   = var.netcup_api_username
  api_password   = var.netcup_api_password
  scripts_dir    = "${path.root}/../../../scripts"
}

module "contabo" {
  source   = "../../modules/compute_contabo"
  for_each = { for name, s in local.servers : name => s if s.provider == "contabo" }

  name           = each.key
  server_id      = try(each.value.external_id, each.key)
  spec_path      = local.contabo_spec
  enabled        = fileexists(local.contabo_spec)
  firewall_rules = local.netcup_rules
  api_username   = var.contabo_api_username
  api_password   = var.contabo_api_password
  scripts_dir    = "${path.root}/../../../scripts"
}

# any-ssh (registry/compute.yaml, the `slots.compute` default): the server
# already exists and the operator applies the firewall by hand. The module has
# no provider resources; wiring it keeps the contract explicit and surfaces the
# server in the outputs (spec §4).
module "any_ssh" {
  source   = "../../modules/compute_any_ssh"
  for_each = { for name, s in local.servers : name => s if s.provider == "any-ssh" }

  name = each.key
  ip   = try(each.value.ip, "")
}
