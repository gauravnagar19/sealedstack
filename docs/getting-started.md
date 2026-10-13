# Getting started

From nothing to a sealed server running your first app, **without installing
anything**. You need a browser, a GitHub account and the accounts listed in
[prerequisites](prerequisites.md). The engine runs in your own GitHub Actions; you
only click, read and merge.

This page is the short path. Every screen, comment and message is shown in the
[user experience](user-experience.md); this page links there instead of repeating
it.

> **Status.** sealedstack is pre-release and has not run against live accounts
> yet (see the [README](../README.md#providers)). The steps below are the signed-off
> flow. Where the current implementation still differs from it, this page says so.

| Time | |
|---|---|
| First setup, including creating accounts and tokens | about 45 minutes of your time |
| `up`: empty server to a running `hello` app | 2 clicks, about 15 minutes |

## The stack

sealedstack is assembled from **slots**, one implementation per external
dependency. This guide uses the **golden path**:

| Slot | Golden path |
|---|---|
| compute | an existing server (`netcup` or `any-ssh`), or `hetzner` (created for you) |
| mesh | tailscale |
| ingress and gate | Cloudflare Tunnel and Cloudflare Access |
| secrets | Bitwarden Secrets Manager (read-only) |
| backup and state | R2 |
| platform | Dokploy |
| registry | GHCR |
| user auth | Clerk (only for apps that use it) |
| forge and CI | GitHub and GitHub Actions |

Other implementations exist but are experimental. See
[providers](providers.md). If you pick one, use its credentials instead of the
golden-path ones.

## 1. Create the infra repo

Create an **empty private repository** with any name (the examples use
`my-infra`). In its settings, enable **Allow GitHub Actions to create and approve
pull requests**. This is needed once, so the first Setup PR can be opened before
the bot identity exists.

## 2. Add two files

Commit both to `main`.

`sealedstack.version`

```
<version>
```

`.github/workflows/sealedstack.yml`

```yaml
name: sealedstack
on:
  workflow_dispatch:
    inputs:
      command:
        type: choice
        options: [setup, check, plan, apply, up, add-server, add-app, status, restore, drill, drift]
      args:
        description: "e.g. --name server-1 (IP and root password come from the secret source)"
        required: false
  pull_request:
  push:
    branches: [main]
  issue_comment:
    types: [created]
  schedule:
    - cron: "17 * * * *"          # hourly status + drift
jobs:
  sealedstack:
    uses: <owner>/sealedstack/.github/workflows/infra.yml@v<version>
    with:
      command: ${{ inputs.command }}
      args: ${{ inputs.args }}
    secrets: inherit
    permissions: { contents: write, pull-requests: write, issues: write, id-token: write, packages: read }
```

The version appears twice: in `sealedstack.version` and in the workflow ref
(`@v<version>`), because GitHub needs a fixed ref there. `check` fails if they
differ, and the upgrade bot always bumps both in one pull request.

## 3. Run Setup

**Actions, sealedstack, Run workflow, command `setup`**, with these `args`:

```
--domain example.com --email you@example.com --admin admin
--server server-1 --provider netcup --secrets bitwarden
```

Setup asks for nothing else. It opens **PR #1 "sealedstack setup"** with
`config/stack.yaml`, `config/servers.yaml`, `config/apps.yaml` (containing a
`hello` app), `.devcontainer/devcontainer.json` and a README, and posts the living
checklist as a comment ([example](user-experience.md#step-4-the-setup-pr-and-its-live-checklist)).

Setup writes the **name of every secret** into `config/`, so you can always see
which Bitwarden entry feeds which setting
([details](user-experience.md#step-3b-the-secret-names-are-written-into-your-config-d-050)).
You can rename any key, for example to reuse an existing entry.

## 4. Create the prerequisites

Work down the checklist. Each item links to the exact page and the exact
permission list. The full list, with the exact key names, is in
[prerequisites](prerequisites.md). In short:

1. **Cloudflare.** Add your domain to Cloudflare, enable Zero Trust, and create
   one API token with the [permissions listed here](prerequisites.md#cloudflare-api-token-permissions).
2. **Tailscale.** Turn on MagicDNS and create an OAuth client with the scopes
   `policy_file`, `auth_keys` and `devices:core`.
3. **Bitwarden Secrets Manager.** Create a project (default `infra`) and a
   **read-only machine account** for it. Put the Cloudflare token, the Tailscale
   OAuth client, a state passphrase (at least 16 characters) and your server
   credentials into that project. Only the machine account's access token goes
   into GitHub, as the repository secret `BWS_ACCESS_TOKEN`.
4. **Your server.** For an existing machine (Netcup, any-ssh), add its public IP
   and root password to Bitwarden under the key names Setup wrote to
   `servers.yaml` (`SERVER_IP_SERVER_1` and `ROOT_PASSWORD_SERVER_1` here), and
   open TCP 22 at the provider firewall temporarily. A provider with an API
   (Hetzner) needs only its token. See the [Netcup firewall](netcup-firewall.md)
   guide.
5. **The bot identity.** Click the checklist's *Create the token* link. It opens
   GitHub's fine-grained token form with the name, owner, expiry and permissions
   already filled in; limit it to this repository and your app repositories,
   create it and store it as the repository secret `GH_TOKEN`
   ([permissions](prerequisites.md#bot-identity-fine-grained-token), D-058). The
   very first Setup PR is opened with the built-in token; later pull requests use
   the bot, because only a bot's pull requests start the plan workflows.
6. **Your SSH public key** goes into `config/stack.yaml` under `admin.ssh_keys`
   (it is not a secret).

Comment **`/sealedstack check`** on the PR whenever you like. When the Tailscale
OAuth client exists, `check` imports your current tailnet policy into
`config/tailscale-policy.hujson` as a new commit on the Setup branch, so you can
review it. If the policy still has Tailscale's default allow-all rule, the
checklist explains how to narrow it (`mesh.narrow_allow_all: true`).

## 5. Merge PR #1, then run Up

When every item is green, **merge PR #1**. That is your approval of the
configuration. Nothing outside the repository has changed yet.

Then **Run workflow, command `up`**. The job waits for approval (an approver listed
in `stack.yaml: approvers`, as described in the
[user experience](user-experience.md#121-commands-ci-input-pr-comment-and-local-all-the-same)),
and then reports its progress to the job summary and the Status issue
([example](user-experience.md#step-7-run-up)). If a step fails, the summary names
the step, the cause and the fix, and **running `up` again continues** where it
stopped.

`up` also creates the Dokploy admin account and an API key itself, through
Dokploy's API over the tailnet. The generated admin password and the key live only
in the encrypted state. If you ever need the admin login, run
`sealedstack status --show-dokploy-admin --break-glass` in a local session (it is
refused in CI and shows the password once).

## 6. Verify

`up` verifies this itself and prints the result. To check by hand:

```sh
curl -sI https://hello.example.com | head -1     # 302 to the Access login
curl -sI https://hello.example.com/.env | head -1 # 403
nc -zv <public-ip> 22 80 443 3000                 # all must time out
```

From a machine on your tailnet, `ssh server-1` works and
`http://server-1:3000` shows the Dokploy panel. Re-running `up` afterwards
reports no changes.

When the first run is green, **close TCP 22 at the provider firewall** and delete
`SERVER_IP_SERVER_1` and `ROOT_PASSWORD_SERVER_1` from Bitwarden. The server is
reached only by its tailnet name from then on.

## What next

| I want to | Do this | Details |
|---|---|---|
| Add another server | Run workflow `add-server --name server-2` (existing machines) or add it to `servers.yaml` in a PR (API providers) | [user experience, journey B](user-experience.md#5-journey-b-add-another-server) |
| Add an app | Run workflow `add-app --name example-app --repo you/example-app --host app.example.com --access owner-only`; merge the app PR first, then the infra PR | [journey C](user-experience.md#6-journey-c-add-an-app) |
| Release or roll back an app | In the app repo: Actions, release, Run workflow, tag `v0.0.N` | [journey D](user-experience.md#7-journey-d-release-and-roll-back-app-developer) |
| Change configuration | Edit `config/` in a pull request, read the plan comment, merge, approve | [journey E](user-experience.md#8-journey-e-day-2-changes) |
| Upgrade sealedstack | Merge the bot's version-bump PR | [journey F](user-experience.md#9-journey-f-upgrade-sealedstack) |
| Recover from a problem or restore a database | Follow the Status issue comment; `restore` and `drill` | [journey G](user-experience.md#10-journey-g-when-something-goes-wrong) |
| Use GitLab instead | Swap the workflow file for the CI include | [journey H](user-experience.md#11-journey-h-moving-to-gitlab) |

## Optional: a terminal in a Codespace

Setup generates `.devcontainer/devcontainer.json` pinned to the engine image.
Opening the infra repo in a Codespace gives you a terminal with `sealedstack`
ready:

```sh
sealedstack check      # preflight, read-only
sealedstack plan       # read-only
sealedstack status     # read-only
```

Writes (`apply`, `up`, `server add`, `db restore`) are refused outside CI unless
you pass `--break-glass`, which also requires a clean checkout equal to
`origin/main`, takes the state lock, asks you to type the infra name and posts an
audit line to the Status issue
([break-glass](user-experience.md#break-glass-github-or-ci-is-down)).

## Troubleshooting

Every failure is reported as *what happened, why, the fix and a link*
([message format](user-experience.md#123-message-format-every-error-everywhere)).
The common ones:

- **A checklist item is red because a secret is "not found".** The message names
  both the logical secret and its key in the secret source. Add that key to the
  Bitwarden project, or change the mapping in `config/stack.yaml: secrets.keys`.
- **`BWS_ACCESS_TOKEN` is not set.** Add it as a repository secret (see step 4).
- **The version pins do not match.** Make `sealedstack.version` and the workflow
  `@v<version>` ref equal in one pull request.
- **The server is unreachable over Tailscale.** Check that the node joined with
  the right hostname and that MagicDNS is on. See [limitations](limitations.md).
- **The first bootstrap cannot connect.** Confirm TCP 22 is open at the provider
  firewall and that the IP and password keys in Bitwarden match `servers.yaml`.
