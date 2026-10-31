> **Maintainers.** This page is for changing sealedstack itself. If you are setting up
> or running a sealedstack infrastructure, you want the guides in [docs/](../README.md).

# Adding a provider

The framework is registry-driven, so most of the work is one entry plus code
for the provider itself.

1. **Register it** in `registry/<slot>.yaml`:

   ```yaml
   - id: coolify
     name: Coolify
     status: planned            # flip to native/adapter when implemented
     creates_server: false
     summary: Self-hosted PaaS.
     secrets: [COOLIFY_API_TOKEN]
     config: [endpoint]
     impl: { tofu_module: platform_coolify, stack: platform }
     docs: https://coolify.io/
   ```

2. **Implement it** using the pattern that fits:

   | Pattern | When | Look at |
   |---|---|---|
   | Native OpenTofu module | an OpenTofu provider exists | `tofu/modules/compute_hetzner`, `backup_s3`, `secrets_aws_ssm` |
   | REST adapter | an API exists but no provider | `tofu/modules/compute_netcup` + `engine/sealedstack/firewall.py` |
   | Ansible role | a self-hosted service on the host | `ansible/roles/mesh_tailscale` |
   | CLI adapter | a CLI tool (sops, pass, 1password) | `engine/sealedstack/sopssecrets.py` |

   Wire it into the stack that composes the slot (`tofu/stacks/edge`,
   `tofu/stacks/compute`, `tofu/stacks/platform`).

3. **Add a sample** under `config/providers/<slot>_<impl>.example.yaml` if it
   needs non-secret config, and list the secrets in the registry entry.

4. Run `sealedstack check`.

For how users select and configure providers, see [providers](../providers.md).
