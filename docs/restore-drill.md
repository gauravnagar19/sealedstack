# Restore drills

A backup you have never restored is a hope. A **drill** rehearses the restore: it takes the newest backup of an app's
managed database and restores as much of it as you ask for, in a **separate throwaway Postgres container** on the app's
server (no network, a memory limit, removed when the drill ends). It never writes to the live database and never reads from it,
except for its size before a data level.

Run it by hand whenever you like (Actions, Run workflow, command `drill`, args below), or let the weekly run do it. The weekly
run is **off by default**.

```text
drill demo-app                       # the level from your settings (default: tables)
drill demo-app notes                 # also restore and count the table `notes`
drill demo-app --level schema        # a lighter level, once
drill all                            # every app with a managed database
```

## Levels

Each level includes the ones before it. The report always lists what it **did not** test.

| Level | What it proves | What it cannot prove | Cost |
|---|---|---|---|
| `verify` | the whole file is intact (gzip checksum) and the archive is readable | that anything restores | no database, no disk |
| `schema` | the structure applies on this Postgres version, with its extensions and locale | that any row loads | a few MB |
| `sample` | the first row(s) of every table load: types and encoding | constraints and triggers (switched off for the sample), the rest of each table | small |
| `tables` (default) | the full data of the tables you name loads, and has at least `min_rows` | the other tables, constraints across tables | the size of those tables |
| `full` | everything restores, constraints and triggers included | whether it is the data you expect | the size of the database |

Disk and time are never inferred from a sample. The data levels (`tables`, `full`) first compare the free disk on the server
with the dump (`tables`) or the live database (`full`), times `min_free_ratio`, and **stop with a clear message instead of
restoring** when there is not enough room. `schema` and below need almost nothing.

## Settings

Infra repo, `config/stack.yaml`:

```yaml
drill:
  scheduled: false       # true: the weekly cron (Sunday 03:00 UTC) runs `drill all`
  level: tables          # verify | schema | sample | tables | full
  min_free_ratio: 1.2    # free disk needed, as a multiple of the dump or the live database
  memory: 1g             # limit of the throwaway container
```

Per app, `config/apps.yaml`:

```yaml
apps:
  demo-app:
    drill:
      enabled: true      # false: skipped by `drill all` and the weekly run
      level: tables      # overrides the stack level for this app
      tables: [notes]    # the tables whose full data the `tables` level restores and counts
      min_rows: 1
      sample_rows: 1
```

An argument on the command line wins over the app, and the app over the stack.

## Limits

- The dump is held in memory and sent to the server over SSH, so drills refuse a backup larger than 2 GiB (streaming is a
  follow-up). Use `verify` and check the size trend until then.
- The throwaway container uses the same Postgres image version as the app's database, plain Postgres: an app that needs an
  extension that image does not ship (PostGIS, for example) fails at `schema`, which is the finding.
- The weekly cron line must be in your workflow file (the template has it); `drill.scheduled` decides whether it does anything.
