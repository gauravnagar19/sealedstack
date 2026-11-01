# Backups and restore drills

Dokploy backs up each app's managed Postgres database to your R2 bucket on the schedule in the app's settings
(`sealedstack.yaml`, for example every 6 hours). sealedstack does not make the backups. It lists them, checks that they can
be restored, and restores one when you ask (D-084, D-085).

A backup you have never restored is a hope. A **drill** rehearses the restore: it takes the newest backup of an app and
restores as much of it as the level asks for, into a **separate throwaway Postgres container**. It never writes to the live
database and never reads from it, except for its size before the `full` level.

## Commands

All backup commands share one noun, `backup`. `drill` and `restore` still work as aliases for one release.

| Command | What it does | Writes? |
|---|---|---|
| `backup list` | each app's newest backup in R2 | No |
| `backup check <app> [table] [--level L] [--where W]` | a drill (alias: `drill`); `<app>` can be `all`, `W` is `server` or `runner` | Throwaway container only |
| `backup restore <app> [backup-key] [--force]` | restores a backup into the live database (alias: `restore`) | Yes |

Run them from **Actions, sealedstack, Run workflow**, command `backup`, with the rest in `args`, or as a comment on a pull
request or issue:

```text
check demo-app                       # args for command `backup`: the level from your settings (default: schema)
check demo-app notes --level tables  # restore and count the table `notes`, once
check all                            # every app with a managed database
/sealedstack backup check demo-app   # the same as a comment
```

**Who may run a drill.** Only the approvers in `config/stack.yaml: approvers` (R7). A drill uses the server's memory and CPU
and reads a production backup, so other collaborators get a refusal. It needs no plan step and no `production` approval.

**Off by default.** A fresh setup runs no drill on its own. An approver can always run one by hand; the weekly run starts only
when you [turn it on](#turn-on-the-weekly-drill).

## Levels

Each level includes the ones before it. The default is `schema`. The report always lists what it **did not** test.

| Level | What it proves | What it cannot prove | Needs |
|---|---|---|---|
| `verify` | the whole file is intact (gzip checksum), the archive is readable, its entry count | that anything restores | no database, no disk |
| `schema` (default) | the structure applies on this Postgres version, with its extensions and locale | that any row loads | about 1 GiB of disk, memory for a small Postgres |
| `sample` | the first row(s) of every table load: types and encoding | constraints and triggers (switched off for the sample), the rest of each table | small |
| `tables` | the full data of the tables you name loads, and has at least `min_rows` | the other tables, constraints across tables | the size of those tables |
| `full` | everything restores, constraints and triggers included | whether it is the data you expect | the size of the database |

## Where it runs

| | `where: server` (default) | `where: runner` |
|---|---|---|
| Levels | all five | `verify` and `schema` only |
| What leaves the server | nothing | the table structure (names, columns, indexes, functions), no rows |
| Size limit | none | none |
| Load on the server | the throwaway container | one short container without a database |

**Server.** The engine, running in CI, creates a presigned URL for that one backup object: read-only, valid 15 minutes. It
hands the URL to the server over SSH on standard input, never on a command line. The server downloads the backup itself and
streams it into the throwaway container. No backup byte passes through the GitHub runner, and nothing is held whole in memory.

**Runner.** The server checks the file (`verify`) on the stream, then sends only the structure (`pg_restore --schema-only`, SQL
text without rows) to the GitHub runner, which applies it to its own throwaway Postgres. No row leaves the server; table and
column names do. Choose it when the server is too small for a drill.

```text
server:  R2 --presigned URL--> server --gunzip, checksum--> throwaway container
runner:  R2 --presigned URL--> server --verify, structure only--> GitHub runner --> throwaway Postgres
```

## Before a drill starts

The drill measures the place it will run. If a limit is not met, the outcome is **skipped**, not failed, and nothing starts.

| Check | Server | Runner |
|---|---|---|
| Free memory (`MemAvailable`) | at least 2 x `limits.memory` | the same |
| CPU load (1-minute load per core) | at most `limits.max_load` | not checked |
| Free disk (Docker root) | `verify`: none; `schema`, `sample`: 1 GiB; `tables`: `min_free_ratio` x the dump; `full`: `min_free_ratio` x the live database | the same, on the runner's disk |
| Postgres image present | yes, or `limits.allow_image_pull: true` | pulled (the runner is disposable) |
| Docker answers | yes | yes |
| No other drill running on this server | yes, one at a time | not needed |

The throwaway container has no network, the limits `memory`, `cpus` and a process limit, a low I/O weight, and the label
`sealedstack.drill=1`. It is removed when the drill ends, and killed after `limits.timeout` (default 30 minutes). Before a
drill starts, any labelled container older than the timeout is removed, so a runner that died mid-drill leaves nothing behind
for long.

## Results

| Outcome | Means | Job |
|---|---|---|
| passed | the level succeeded | green |
| failed | the backup or the restore is broken | red, and a comment on the Status issue |
| skipped | a preflight limit was not met, or the app has `drill.enabled: false` | green, with a warning |

Every report lists what was tested, what was not, and what left the server. It also gives the backup's age and warns when it
is older than `max_backup_age` (default 48 hours): a drill of a stale backup proves little. The newest result per app shows in
the [Status issue](user-experience.md#the-status-issue-always-pinned): when, level, place, outcome.

## Settings

Infra repo, `config/stack.yaml`:

```yaml
drill:
  scheduled: false       # true: the weekly cron (Sunday 03:00 UTC) runs `backup check all`
  level: schema          # verify | schema | sample | tables | full
  where: server          # server | runner (runner: verify and schema only)
  min_free_ratio: 1.2    # free disk needed, as a multiple of the dump or the live database
  max_backup_age: 48h    # warn when the newest backup is older
  limits:
    memory: 1g           # of the throwaway container; the preflight wants twice this free
    cpus: 1
    timeout: 30m         # the container is killed after this
    max_load: 0.7        # 1-minute load per core, server only
    allow_image_pull: false  # true: pull the Postgres image if the server does not have it
```

Per app, `config/apps.yaml`:

```yaml
apps:
  demo-app:
    drill:
      enabled: true      # false: skipped by `backup check all` and the weekly run
      level: tables      # overrides the stack level for this app
      tables: [notes]    # the tables whose full data the `tables` level restores and counts
      min_rows: 1
      sample_rows: 1
```

A flag on the command line wins over the app, and the app over the stack.

## Turn on the weekly drill

1. In a pull request, set `drill.scheduled: true` in `config/stack.yaml` and merge it.
2. Keep the weekly cron line in `.github/workflows/sealedstack.yml` (the template has it):
   `- cron: "0 3 * * 0"`. Without `drill.scheduled: true` it does nothing.

The weekly run checks every app with a managed database at its own level and place.

## Restore a backup

`backup restore <app> [backup-key] [--force]` is a write command: it needs an approver, the `production` approval and the
app name typed to confirm, like the other writes ([approval](user-experience.md#121-commands-ci-input-pr-comment-and-local-all-the-same)).
Without a key it takes the newest backup. It refuses a database that is not empty unless you add `--force`. The app is stopped
during the restore and restarted after. The backup is streamed through a presigned URL, as for a drill, so it never passes
through the runner and has no size limit.

## Limits

- The throwaway container uses plain Postgres of the same major version as the app's database. An app that needs an extension
  that image does not ship (PostGIS, for example) fails at `schema`, which is the finding.
- `where: runner` tests `verify` and `schema` only. Data levels always run on the server.
- Each step of a level streams the backup from R2 again, so a drill moves about the backup size times the number of steps.
- See also [known limitations](limitations.md#backups-and-restore-drills).
