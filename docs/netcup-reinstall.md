# Wipe and reinstall a Netcup server from CI

`reinstall` erases a Netcup server's disk and installs a fresh Ubuntu or Debian through Netcup's
Server Control Panel (SCP) REST API, so a rebuild-from-nothing run needs no clicking in the
provider panel. It is a guarded write command: it runs only from **Actions, Run workflow**, only
for an approver, and only when you type the server's name again. It exists for Netcup only. Other
providers create and destroy servers through OpenTofu.

> **Status.** New in D-070 and not yet run against a live account ([limitations](limitations.md#netcup-reinstall-has-not-run-live-d-070)).
> Read the dry-run output before the real one.

## What you set up once

| What | Where | Notes |
|---|---|---|
| `external_id` | `config/servers.yaml` | The server's id or `v22…` name from the Server Control Panel. |
| `os` (optional) | `config/servers.yaml` | `ubuntu-24.04` (default), `debian-13`, `debian-12` and so on. Netcup names its images like `Debian 13.7.0 UEFI amd64`; sealedstack matches the OS and version, then picks the build that fits the server: plain OS images only (not `cloudimg` or other products), the server's own boot mode (UEFI or BIOS, required: a mismatch could not boot), its architecture, and the `minimal` variant. If one image is still not clear, the run lists them and you pass `--flavour <id>`. |
| Root password | secret source: `ROOT_PASSWORD_<NAME>` | **You choose it**: at least 12 printable characters, no spaces. sealedstack cannot write to your secret source, so the new install is given exactly this password, and the next `up` logs in with it (D-061). |
| Console password (optional) | secret source: `CONSOLE_PASSWORD_<NAME>` | What the Netcup console accepts for `root` once `up` has run, so the bootstrap password above stops being the key to the machine ([prerequisites](prerequisites.md)). |
| Server IP | secret source: `SERVER_IP_<NAME>` | Unchanged by a reinstall. Used only to see when port 22 answers. |
| Netcup refresh token | secret source: `NETCUP_REFRESH_TOKEN` | See below. |
| Tailscale OAuth client | secret source (already there) | Needs the `devices:core` scope so the old tailnet node can be removed. Without it the run tells you to delete the node by hand. |

Netcup's firewall must allow inbound TCP 22 for the first login ([Netcup firewall](netcup-firewall.md)).

## Create the Netcup refresh token

Netcup signs API users in with a browser approval ("device flow"). Do it once from any terminal
with `curl`; the token it prints is the secret, so paste it straight into your secret source.

```bash
curl -s -X POST https://www.servercontrolpanel.de/realms/scp/protocol/openid-connect/auth/device \
  -d client_id=scp -d 'scope=offline_access openid'
```

Open the `verification_uri_complete` it returns, sign in with your Netcup customer number and
SCP password, and approve. Then, within ten minutes, exchange the `device_code` it printed:

```bash
curl -s -X POST https://www.servercontrolpanel.de/realms/scp/protocol/openid-connect/token \
  -d grant_type=urn:ietf:params:oauth:grant-type:device_code -d client_id=scp -d device_code=<device_code>
```

Store the `refresh_token` value as `NETCUP_REFRESH_TOKEN`. The key name can be changed with
`secrets.keys.netcup_refresh_token` in `config/stack.yaml`.

### Renewing the token

The token is an offline token: it keeps working while it is used and lapses after about 30 days
of not being used. Every use returns a fresh token string, but the one you stored stays valid, so you never have to update the secret after a run. When `reinstall` says Netcup refused it, repeat the two commands and replace
the secret.

## Run it

1. **Actions, sealedstack, Run workflow**: command `reinstall`, args `--name server-1 --dry-run`.
   The summary names the Netcup server, the disk and the image it would erase. Nothing changes.
2. Run it again with `--name server-1 --confirm server-1`. A mismatched or missing `--confirm`
   refuses before anything is touched.
3. The job starts Netcup's image install, waits for it (a few minutes), removes the old
   `server-1` node from your tailnet, clears the saved `up` progress, and waits for port 22.
4. Run `up`. It sees no tailnet node, so it bootstraps the new install with the root password.

Options: `--flavour <id>` picks an image by Netcup's id, `--disk <dev>` the disk to wipe when the
server has more than one.

## What a reinstall does not clean up

- State in OpenTofu (Cloudflare tunnels, DNS, Access, R2) is unaffected and is reused.
- Dokploy's admin and API key kept in the encrypted state belong to the old panel. `up` finds the
  stored key no longer works and creates new ones.
- If `bootstrap.host_key_fingerprint` is pinned, the new install has a new host key: replace it
  with the fingerprint the Server Control Panel shows, or remove the line. The run reminds you.
- Database backups in R2 stay. Restoring a database onto the new server is a separate step
  (`restore`).
