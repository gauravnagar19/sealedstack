# Security hardening

This page covers the roadmap "Security hardening" items in
roadmap that are implemented in this repo, and the ones that
remain operational practice. The threat model they extend is in
[spec.md §13](spec.md#13-security-model).

The origin is already sealed by design: zero inbound ports, a private mesh,
and an outbound-only tunnel. The controls below are **defence in depth** on top
of that; none of them replaces it.

| Control | Where | Status |
|---|---|---|
| Zero inbound ports + `DOCKER-USER` | `firewall` role (D-005) | done |
| Least-privilege Tailscale ACL | `tofu/stacks/edge/main.tf` (D-014) | done |
| gitleaks | CI + pre-commit | done |
| CIS benchmark baseline | `ansible/roles/cis_baseline` | this page |
| CrowdSec + firewall bouncer | `ansible/roles/crowdsec` | this page |
| SBOMs + image scanning | app CI (`security.yml` template) | this page |
| Audit logging | Cloudflare + Dokploy settings | this page |
| FIDO2 hardware keys | operator practice | this page |
| Secret rotation | `engine/sealedstack/rotator.py` | referenced below |

## Adding the two Ansible roles

Both roles are idempotent and safe to re-run with `just server-sync`. Add them
to the `roles:` list in [`ansible/playbooks/site.yml`](../ansible/playbooks/site.yml)
(shown here relative to the existing entries):

```yaml
    # Security hardening (docs/security-hardening.md). Opt-in hardening.
    - role: cis_baseline
      when: cis_baseline_enabled | default(true) | bool
    - role: crowdsec
      when: crowdsec_enabled | default(false) | bool
```

The integrator owns `site.yml`; these roles were delivered without editing it.
Both are controlled from defaults, so they can also be enabled per host with
`group_vars` instead of editing the playbook:

| Role | Toggle | Default |
|---|---|---|
| `cis_baseline` | `cis_baseline_enabled` | `true` |
| `crowdsec` | `crowdsec_enabled` | `false` |

## CIS baseline (`cis_baseline`)

A small, safe subset of the CIS Benchmark. It is deliberately **not** a full
CIS run: it touches only things that cannot cut the mesh, the tunnel or the
Docker data path.

It does:

- **Kernel/sysctl hardening** — restrict `dmesg` and `kptr`, `ptrace_scope`,
  ASLR, core dumps, `suid_dumpable`.
- **Network hardening** — `rp_filter`, source routing, ICMP redirects, martian
  logging, `tcp_syncookies`, and the IPv6 equivalents.
- **Filesystem/module blacklist** — rarely-used filesystems (`cramfs`,
  `freevxfs`, `jffs2`, `hfs`, `hfsplus`, `udf`) and unused protocols (`dccp`,
  `sctp`, `rds`, `tipc`) via `/etc/modprobe.d`, then rebuilds the initramfs.
- **Account database permissions** — `0644` on `/etc/passwd` and `/etc/group`,
  `0640 root:shadow` on `/etc/shadow` and `/etc/gshadow`, including the `-`
  backups when present.

It does **not** touch:

- **SSH or crypto policy** — owned by the [`ssh` role](../ansible/roles/ssh/tasks/main.yml)
  (spec [§7.1–7.2](spec.md#71-bootstrapyml)).
- **Firewall, routing or `ip_forward`** — owned by the
  [`firewall` role](../ansible/roles/firewall/tasks/main.yml). `ip_forward` must
  stay `1` for Docker, so it is intentionally absent.
- **Docker daemon settings** — owned by the `docker` role.

Files written (see `ansible/roles/cis_baseline/defaults/main.yml`):

| Path | Purpose |
|---|---|
| `/etc/sysctl.d/99-sealedstack-cis.conf` | sysctl baseline |
| `/etc/modprobe.d/sealedstack-cis-blacklist.conf` | module blacklist |

`ansible/roles/cis_baseline/defaults/main.yml` holds the full sysctl map, the
module list and the account-file specs. `squashfs` (used by snapd) and
`usb-storage` are excluded on purpose; add `usb-storage` for hosts that will
never see removable media.

## CrowdSec (`crowdsec`)

Installs [CrowdSec](https://www.crowdsec.net/) and the iptables firewall
bouncer as Docker Compose services (target hosts already run Docker):

- `crowdsec` — the detection agent, with `crowdsecurity/linux` and
  `crowdsecurity/sshd` collections, keeping config and data in named volumes.
  Its Local API is published on `127.0.0.1:8080` only.
- `crowdsec-firewall-bouncer` — runs with `network_mode: host` and `NET_ADMIN`
  to program iptables from CrowdSec decisions. The role registers a bouncer
  API key once and stores it on the host.

**Read this before enabling it.** CrowdSec analyses traffic that *reaches the
host*. A sealed origin has zero inbound ports (spec [§13](spec.md#13-security-model)),
so CrowdSec should only ever see traffic that already bypassed the provider
firewall, UFW and the `DOCKER-USER` chain — which should be none. It is defence
in depth and source-IP reputation, not a primary control, so
`crowdsec_enabled` defaults to `false`. Evaluate whether it earns its memory
and container count before turning it on.

Images are pinned to exact tags in `ansible/roles/crowdsec/defaults/main.yml`
(spec [§13](spec.md#13-security-model), supply chain); bump them through
Renovate.

## Software supply chain: SBOMs and image scanning

Every app should generate an SBOM and scan its image on every release. The
template is
[`examples/app-template/.github/workflows/security.yml`](../examples/app-template/.github/workflows/security.yml):
it builds the image, generates a CycloneDX/SPDX SBOM with
[anchore/sbom-action](https://github.com/anchore/sbom-action) (which wraps
[syft](https://github.com/anchore/syft)) and scans with
[aquasecurity/trivy-action](https://github.com/aquasecurity/trivy-action),
failing on `HIGH` and `CRITICAL`. Copy it into each app repo's
`.github/workflows/` and adjust the image build for that repo.

To scan locally, before pushing:

```sh
syft dir:. -o spdx-json > sbom.spdx.json
grype sbom:sbom.spdx.json --fail-on high
# or, straight against an image:
trivy image --severity HIGH,CRITICAL --exit-code 1 <image>:<tag>
```

Pair this with the existing pinning + Renovate policy (spec
[§13](spec.md#13-security-model)): a result is only actionable if the base image
and dependencies are pinned to exact versions.

## Audit logging

Access and WAF decisions are made at Cloudflare's edge, before traffic reaches
the origin. What you can retain depends on the plan:

| Source | Free plan | Notes |
|---|---|---|
| Cloudflare Access authentication logs | **24 hours** | Visible in the Zero Trust dashboard. |
| Cloudflare Logpush / Logpull | **Paid** | The supported way to export Access and firewall events to R2/S3. |
| WAF / firewall events | Short retention | Sample or export on a paid plan. |
| Dokploy panel + container logs | Local only | Bounded by Docker log rotation (`max-size=10m`, `max-file=3`). |

On the free plan there is no supported bulk export, so treat the 24-hour
dashboard view as the audit surface and add external uptime/health checks
([operations.md](operations.md) "monitoring and alerting", still planned). If
you move to a paid plan, enable **Logpush of Access requests to the
`sealedstack-backups` R2 bucket** (spec [§8](spec.md#8-edge-opentofu-stacksedge))
so authentication events survive independently of the host.

## FIDO2 hardware keys

Hardware-backed keys remove the private-key-on-disk threat (spec
[§6](spec.md#6-secrets-model), "stolen laptop").

### SSH

Generate a resident, hardware-backed key and require it for verification:

```sh
ssh-keygen -t ed25519-sk -O resident -O verify-required -C "admin@sealedstack"
```

- `-O verify-required` forces a PIN/touch per use; if the box is ever
  reachable with a password, keep it off (`PasswordAuthentication no`, spec
  [§7.1](spec.md#71-bootstrapyml)).
- The credential can be loaded into the Bitwarden SSH agent, so the private
  half is never written to disk (spec [§6](spec.md#6-secrets-model)).
- Optionally add a second `sk` key as a backup and keep it in a safe.

SSH policy itself lives in the [`ssh` role](../ansible/roles/ssh/tasks/main.yml);
this is an operator key change, not a role change.

### Bitwarden vault

- Enable **two-step login with a passkey / WebAuthn security key** and remove
  weaker factors.
- If you are on an organisation, enforce the **master password + passkey**
  policy for members so a stolen password alone is not enough.
- Keep the break-glass recovery code offline.

## Secret rotation

Rotation is owned by `engine/sealedstack/rotator.py` (delivered by the secrets
ticket; not part of this change). Use it as the single entry point rather than
rotating by hand. Rotate, in rough order of blast radius (roadmap
"Security hardening"):

| Secret | Where it lives |
|---|---|
| Cloudflare API token | Bitwarden Secrets Manager, project `infra` |
| Tailscale API credentials / auth keys | Bitwarden Secrets Manager, project `infra` |
| Database passwords | Dokploy-managed; read by the platform stack |
| Tunnel tokens (per app) | Bitwarden Secrets Manager, `infra.tunnel.<app>` |
| `BWS_ACCESS_TOKEN` | Operator shell only (spec [§6](spec.md#6-secrets-model)) |

After rotating an auth key or tunnel token, re-run `just apply` so the edge
stack picks up the new value; after rotating `BWS_ACCESS_TOKEN`, update it in
your shell and the machine account's token list. The spec's recovery rule still
holds: git + Bitwarden + the R2 bucket are the only inputs needed to rebuild.
