# Full rebuild drill

Everything in a sealedstack is rebuildable from three sources: **git** (config),
**Bitwarden** (credentials) and **R2** (data backups). This is the end-to-end
drill; run it periodically to prove recovery still works.

## Before you start

- The domain is still on Cloudflare, and Zero Trust is enabled.
- Bitwarden Secrets Manager still holds the `infra` keys
  (see [prerequisites](prerequisites.md)), and you have `BWS_ACCESS_TOKEN`.
- The R2 bucket still holds the backups.

## Rebuild a host from nothing

```sh
# 1. Create a fresh box in the provider console and note its IP.
#    (Hetzner/Hostinger can also be created by `just server-up` — see v0.4.)

# 2. Recreate the encrypted state bucket (local state, run once).
export BWS_ACCESS_TOKEN=...
just state-init

# 3. Bootstrap the host and join the mesh.
just server-up server-1   # IP comes from SERVER_IP_SERVER_1 (bootstrap secret)

# 4. Rebuild the edge and the platform from git + the rendered snapshot.
just apply
#    Any database with restore_on_create: latest is restored automatically
#    afterwards (sealedstack.postapply).
```

## Verify

```sh
# Zero inbound ports still holds:
nc -zv <public-ip> 22 80 443 3000      # all must time out

# The app answers, gated by Access:
curl -sI https://hello.example.com | head -1     # 302 to Access

# Idempotent:
just server-sync && just apply && just plan           # no changes
```

## Restore a database on demand

```sh
just app-restore shop                 # latest backup
just app-restore shop 2026-10-11T03:00:00Z.sql.gz   # a named object
```

## Restore drill

Restores the latest dump into a throwaway database, checks it, drops it:

```sh
just db-drill shop users
```

Schedule this weekly (cron, CI, or a Dokploy schedule) and alert on a non-zero
exit. The Dokploy panel's own database is backed up daily to `_dokploy/` and
restored the same way.
