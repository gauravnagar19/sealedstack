# Choosing a stack

sealedstack is assembled from **slots**: one implementation per external dependency
(where the server comes from, the private mesh, the tunnel, the gate, the secret source,
backups, the deploy platform). You choose each in `config/stack.yaml`, and the engine
refuses what it cannot do. This page explains how the project guides that choice and
what to expect when you leave the golden path.

> **Status.** No implementation is `supported` yet: every one is `experimental` (code
> complete, never run against a live account) or `planned`
> ([limitations](limitations.md)). The golden path is the one the defaults, examples
> and docs are written for; the others are there so you are not locked in.

## The golden path

Start here unless you have a reason not to.

| Slot | Default | Why |
|---|---|---|
| compute | an existing server (`netcup`, `any-ssh`) or `hetzner` | you bring a box, or the API creates one |
| mesh | `tailscale` | admin access and CI reach servers only over it |
| ingress and gate | `cloudflare` and `cloudflare_access` | outbound tunnel plus a login in front, free tier |
| secrets | `bitwarden` | the default; sealedstack only reads from it |
| backup and state | `r2` | derived from your Cloudflare token, no extra keys |
| platform | `dokploy` | the panel apps are deployed through |
| forge and CI | GitHub and GitHub Actions | the engine runs in your own Actions |

## How the project guides you

You are never asked to guess. Five things tell you what a choice means, in the order you
meet them.

1. **The registry is the catalog.** `registry/<slot>.yaml` lists every implementation
   with its status, a one-line summary, the secrets it needs, the config keys it reads,
   where its code lives and a link to its docs. See it from the CLI:

   ```sh
   sealedstack debug providers            # every slot
   sealedstack debug providers secrets    # one slot
   ```

2. **Status says how much to trust it.** `supported` passed the live acceptance suite,
   `experimental` is code that has never run live, `planned` has no code.
3. **Setup covers the two common swaps.** `--provider` picks the compute provider and
   `--secrets` the secret source (`bitwarden`, `doppler`, `infisical`, `env`, `ci`,
   `sops`). Every other slot is a one-line change in `config/stack.yaml` in a pull
   request, which is where the plan comment shows you what it does.
4. **`check` is the guide.** It reads your choices and then:
   - fails if an implementation is missing or `planned`, so you never get a silent wrong
     deploy;
   - works out the credentials your *chosen* implementations need, from their registry
     entries, and reports each missing one with the exact key name and where it goes;
   - tells you what to do, not just what is wrong: every message is *what happened, why,
     the fix and a link*.
5. **The checklist makes it a to-do list.** Setup posts a living checklist on PR #1 that
   `/sealedstack check` keeps current. Swapping a slot changes the list; you work down it.

## Slot by slot

| Slot | Keep the default when | Swap when | What changes for you |
|---|---|---|---|
| compute | you have a server | you want the API to create servers (`hetzner` and others with `creates_server`) | credentials: the provider token replaces the server's IP and root password |
| mesh | you use Tailscale | you self-host (`headscale`, `netbird`, ...) | a controller to run; these are Ansible roles, less exercised |
| ingress, gate | you are on Cloudflare | you want your own identity provider (`authentik`, `authelia`, ...) | a service on your host to run and secure |
| secrets | you use Bitwarden | you use Doppler, Infisical, a cloud secret store, `sops` | the one token or identity kept in GitHub changes ([prerequisites](prerequisites.md)) |
| backup | you use R2 | you want another S3-compatible store (`s3`, `b2`, `wasabi`, ...) | `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, plus `endpoint`, `region` and `bucket` |
| platform | you use Dokploy | you prefer `coolify`, `caprover`, `portainer` | Ansible-based and less exercised; the app flow assumes Dokploy |
| user auth | the app uses Clerk | the app uses another provider | the app's own keys, per app |

Two things to know when you swap:

- **The secret source is the one swap with a full doc.** Doppler and Infisical are native
  read-only sources with their own sections in [prerequisites](prerequisites.md#using-doppler-instead-of-bitwarden),
  including how secret names map.
- **Backup and state are separate settings.** `slots.backup` is where app and database
  backups go; `state.backend` is where the encrypted OpenTofu state lives.

## What is *not* checked

Be clear about the limits so an agent or a human does not over-trust a green check:

- **Combinations are not cross-validated.** The engine checks each slot on its own and a
  few known couplings (for example that `slots.mesh: tailscale` needs `mesh.tailnet`).
  It does not know that two experimental choices work well together.
- **A green `check` is not a live test.** It proves the credentials, the names and the
  config line up. It does not prove the provider's API behaves as the code assumes.
- **Only the golden path is exercised by the project's own runs.** Everything else is
  best-effort until it passes the live suite.

## Changing a stack safely

1. Change **one slot at a time**, in a pull request.
2. Run `/sealedstack check` and fix every red item (a new credential, a new config key).
3. Read the plan comment: it lists what will be created, changed or destroyed. A mesh,
   gate or backup swap can touch a lot.
4. Merge, approve, and re-run `status`. Run `drill` after a backup swap.
5. If it fails, the fix is in the failure message; `up` resumes.

Doing this with an agent? See [run sealedstack with an agent](run-with-an-agent.md).

## Adding an implementation

Not listed? Add it: one registry entry plus the code for the provider. The recipe and the
four patterns (OpenTofu module, REST adapter, Ansible role, CLI adapter) are in
[providers](providers.md#add-a-provider).
