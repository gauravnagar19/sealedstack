# Monitoring and alerting

Sealed origins have **no inbound ports**, so the usual "SSH in and look" is not
the monitoring story. There are two layers:

1. **Dokploy's built-in monitoring** on the panel — what is running and how
   healthy it is.
2. An **external uptime check** — a heartbeat that fires when the host or its
   timer goes dark, which the panel cannot report because it *is* the thing
   that went down.

Both feed alerting (email, chat, webhook).

## Dokploy built-in monitoring

The platform slot deploys Dokploy (see [providers.md](providers.md)); its panel
is the first place to look:

- **Server metrics** — CPU, memory, disk and load for each server Dokploy
  manages, on the panel's monitoring view.
- **Service metrics and logs** — per-container CPU/RAM and the live log stream
  for each compose service (the app, the connector, the database).
- **Deployments** — the history and output of every deploy, so a failed release
  is visible without SSH.
- **Postgres backups** — each managed database has a scheduled backup and the
  last run's status (the platform stack creates the schedule; see
  [rebuild-drill.md](rebuild-drill.md)).

Dokploy caps container logs (`max-size=10m`) rather than shipping them
centrally; central log shipping is still on the roadmap.

Dokploy's [notification integrations](https://docs.dokploy.com/docs/core/notifications)
(webhook, Discord, Slack, Telegram, …) cover deployment and backup events. Set
at least one so a failed deploy or backup reaches you.

## External uptime: the `monitoring_healthchecks` role

`ansible/roles/monitoring_healthchecks` installs a small **systemd timer** that
`curl`s a [healthchecks.io](https://healthchecks.io/) ping URL on an interval.
If the pings stop — host down, network cut, timer disabled — healthchecks.io
raises the check and alerts through its integrations.

It is a heartbeat, not a port scan: nothing listens, nothing is exposed. It
pairs with an HTTP check in healthchecks.io (or another uptime service) pointed
at each public `hostnames[]`, which is what tells you an *app* is down while the
host is still up.

### Configuration

The role's defaults (`ansible/roles/monitoring_healthchecks/defaults/main.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `monitoring_healthchecks_url` | `""` (required) | The ping URL, `https://hc-ping.com/<uuid>` |
| `monitoring_healthchecks_interval` | `5min` | How often the timer fires (`OnUnitActiveSec`) |
| `monitoring_healthchecks_timeout` | `10` | `curl --max-time`, seconds |
| `monitoring_healthchecks_retries` | `3` | `curl --retry` |
| `monitoring_healthchecks_unit` | `sealedstack-healthcheck` | systemd unit base name |

Put the URL under the per-implementation config in `config/stack.yaml` (the
`providers:` block is the sanctioned place for non-secret provider config; the
ping URL is a capability, so treat it as a secret and export it instead if you
prefer):

```yaml
providers:
  monitoring_healthchecks:
    url: https://hc-ping.com/<uuid>
```

### Wiring it into the host baseline

The role is gated on that config. In `ansible/playbooks/site.yml`:

```yaml
    - role: monitoring_healthchecks
      when: (stack.providers.monitoring_healthchecks | default({})).url | default('') | length > 0
      vars:
        monitoring_healthchecks_url: "{{ (stack.providers.monitoring_healthchecks | default({})).url | default('') }}"
```

Re-running `just server-sync` installs or updates the units; it is idempotent
and does nothing when no URL is configured.

### Verify

```sh
systemctl list-timers sealedstack-healthcheck.timer
systemctl status sealedstack-healthcheck.service
journalctl -u sealedstack-healthcheck.service --since '1 hour ago'
```

healthchecks.io should show the check as "up" within one interval of the first
ping.

## Alerting

- **healthchecks.io** — create a check per host and per public hostname; route
  its notifications to email and a chat/webhook integration. A "grace period"
  of one interval absorbs a single missed ping.
- **Dokploy notifications** — deployment and backup failures, sent to the same
  chat channel.
- **Drift** — `.github/workflows/drift.yml` already runs a scheduled `tofu
  plan`; have CI fail (and notify) when it reports a diff, so unmanaged drift
  is also an alert.

Turning these into a single `just status` / `just doctor` roll-up is on the
roadmap (Operations) and sketched in

## Related

- [operations.md](operations.md) — the ops status list this page implements.
- [rebuild-drill.md](rebuild-drill.md) — backup/restore and the scheduled drill.
- [providers.md](providers.md) — the `platform` and `ingress` slots.
