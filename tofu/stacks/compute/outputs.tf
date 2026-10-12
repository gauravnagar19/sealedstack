output "ips" {
  description = "Server name -> public IP, for servers sealedstack created."
  value = merge(
    { for name, server in module.hetzner : name => server.ip },
    { for name, server in module.hostinger : name => server.ip },
    { for name, server in module.digitalocean : name => server.ip },
    { for name, server in module.vultr : name => server.ip },
    { for name, server in module.linode : name => server.ip },
    { for name, server in module.scaleway : name => server.ip },
    { for name, server in module.aws : name => server.ip },
    { for name, server in module.gcp : name => server.ip },
    { for name, server in module.azure : name => server.ip },
    { for name, server in module.oracle : name => server.ip },
    { for name, server in module.upcloud : name => server.ip },
    { for name, server in module.ovh : name => server.ip },
    { for name, server in module.civo : name => server.ip },
  )
}

output "managed" {
  description = "Server names whose lifecycle sealedstack managed."
  value = concat(
    [for name, server in module.hetzner : name if server.managed],
    [for name, server in module.hostinger : name if server.managed],
    [for name, server in module.digitalocean : name if server.managed],
    [for name, server in module.vultr : name if server.managed],
    [for name, server in module.linode : name if server.managed],
    [for name, server in module.scaleway : name if server.managed],
    [for name, server in module.aws : name if server.managed],
    [for name, server in module.gcp : name if server.managed],
    [for name, server in module.azure : name if server.managed],
    [for name, server in module.oracle : name if server.managed],
    [for name, server in module.upcloud : name if server.managed],
    [for name, server in module.ovh : name if server.managed],
    [for name, server in module.civo : name if server.managed],
  )
}
