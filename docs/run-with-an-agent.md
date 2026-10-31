# Run sealedstack end to end with an agent

This page is for the person who will set sealedstack up **together with an AI coding
agent** (Claude Code, Codex, Cursor and so on). It says who does what, what to hand the
agent and what to keep from it, and gives every phase a command and a "done when".

> **Status.** sealedstack is pre-release and has not run against live accounts yet
> ([limitations](limitations.md)). Treat the first run as a test and record what
> differs from this page.

The flow itself is the one in [getting started](getting-started.md); this page adds the
split between you and the agent. Which providers to pick is in
[choosing a stack](choosing-a-stack.md).

## Who does what

sealedstack changes things only through pull requests and CI, so an agent fits well: it
edits `config/`, opens pull requests and reads the results. It cannot (and should not)
do the steps that create trust.

| | You | The agent |
|---|---|---|
| Create accounts (Cloudflare, Tailscale, the secret source, GitHub) | yes | no: give it the checklist |
| Create tokens and keys, and store them as repository secrets or in the secret source | yes | no |
| Create the empty private infra repo and its one setting | yes | can write the two files |
| Run Setup | click it, or let the agent run `gh workflow run` | yes |
| Edit `config/*.yaml` (SSH key, tailnet, project ID, extra apps and servers) | review | yes, in a pull request |
| Comment `/sealedstack check`, read the checklist and plan comments | | yes |
| Merge PR #1 and approve `up` (your approval of the configuration) | **yes, always** | no |
| Open and close TCP 22 at the provider firewall | yes | no |
| Diagnose a failed run from the job summary and Status issue | | yes |

Writes are approval-gated by design: `apply`, `up`, `server add` and `db restore` run
only in CI, only for an approver listed in `stack.yaml: approvers` (D-052), and a local
session refuses them without `--break-glass`. An agent that is told to push the change straight through
cannot.

## What to give the agent, and what not

**Give it:**

- The infra repo, with permission to push branches and open pull requests, and to run
  workflows and comment (`gh` logged in as you, or a token limited to that repo).
- This page, [getting started](getting-started.md), [prerequisites](prerequisites.md)
  and [choosing a stack](choosing-a-stack.md).
- Read access to the Actions logs, the pull request comments and the pinned Status
  issue. They say what failed, why and the fix.

**Never give it:**

- Secret values: the secret source's own token (`BWS_ACCESS_TOKEN`, `DOPPLER_TOKEN`,
  `INFISICAL_CLIENT_SECRET`), provider tokens, root passwords. They go into GitHub
  repository secrets and the secret source, typed by you. `config/` holds only the
  *names* of secrets, so the agent never needs a value.
- The bot token (`GH_TOKEN`) or the Cloudflare and Tailscale credentials.

**Know the limit.** On a private repository on GitHub Free, anyone who can push a branch
can add a workflow that reads the repository secrets
([limitations](limitations.md#approvals-on-github-free-private-repositories-what-they-do-and-dont-protect)).
Treat write access for an agent as "can read the secret source's token". That token is
read-only and scoped to the project you made for sealedstack, so keep that project
limited to what sealedstack needs.

## The run, phase by phase

Each phase ends with a check you can verify yourself. `/sealedstack check` is the
repeating one: it re-reads everything and updates the checklist comment.

| # | Phase | Who | Done when |
|---|---|---|---|
| 0 | Pick the stack | you, with the agent | each slot has an implementation and you know its credentials ([choosing a stack](choosing-a-stack.md)) |
| 1 | Accounts, tokens and secrets | you | the secret source holds every key `prerequisites.md` lists for your stack; the one token is a repository secret |
| 2 | Infra repo and the two files | you create the repo; the agent writes `sealedstack.version` and the workflow | the repo has both files on `main` and **Allow GitHub Actions to create and approve pull requests** is on |
| 3 | Run Setup | you or the agent | PR #1 "sealedstack setup" is open with a checklist comment |
| 4 | Work the checklist | the agent edits, you supply values | `/sealedstack check` shows every item green |
| 5 | Merge PR #1, run `up` | you | `up` finished; the Status issue is green |
| 6 | Verify | the agent | the three `curl` checks and `nc` check in getting started give the stated results |
| 7 | Add an app | the agent, you approve | the app answers behind the gate and re-running `up` reports no changes |
| 8 | Prove recovery | you approve | `drill` restores a database into a throwaway one |

### Phase 0: pick the stack

Read [choosing a stack](choosing-a-stack.md). If this is your first run, **take the
golden path** and change one slot at a time later. The agent's job here is to list the
accounts and credentials your choice needs, from the registry:

```sh
sealedstack debug providers secrets     # one slot; omit the slot for all of them
```

### Phase 1: accounts, tokens and the secret source

This is yours. Work down [prerequisites](prerequisites.md): Cloudflare token, Tailscale
OAuth client, a state passphrase of at least 16 characters, the server's IP and root
password, and the secret source's read-only token. Use the exact key names there; Setup
writes them into `config/` so you can rename them.

**Test the secret source before CI does.** Run these yourself; they print names, never
values.

Doppler:

```bash
export DOPPLER_TOKEN='dp.st.…'
curl -s -H "Authorization: Bearer $DOPPLER_TOKEN" "https://api.doppler.com/v3/configs/config/secrets/download?format=json" | python3 -c 'import sys,json; print(sorted(json.load(sys.stdin)))'
```

Infisical (Universal Auth login, then the list call; replace the project ID):

```bash
export INFISICAL_CLIENT_ID='…' INFISICAL_CLIENT_SECRET='…'
TOKEN=$(curl -s -X POST https://app.infisical.com/api/v1/auth/universal-auth/login \
  -d clientId="$INFISICAL_CLIENT_ID" -d clientSecret="$INFISICAL_CLIENT_SECRET" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["accessToken"])')
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://app.infisical.com/api/v4/secrets?projectId=<project-id>&environment=prod&secretPath=/&viewSecretValue=true" \
  | python3 -c 'import sys,json; print(sorted(s["secretKey"] for s in json.load(sys.stdin)["secrets"]))'
```

If a call answers with an error instead of names, send the error text (never the token)
to the agent or to the sealedstack maintainers. Once names print, `/sealedstack check`
(phase 4) tests the real code path.

### Phase 2: the infra repo

Create an empty **private** repository and turn on *Allow GitHub Actions to create and
approve pull requests*. The agent then commits the two files from
[getting started, step 2](getting-started.md#2-add-two-files) to `main`. The version
appears twice (`sealedstack.version` and the `@v<version>` ref); `check` fails if they
differ.

### Phase 3: run Setup

```bash
gh workflow run sealedstack.yml -f command=setup \
  -f args='--domain example.com --email you@example.com --server server-1 --provider netcup --secrets bitwarden'
```

`--secrets` takes `bitwarden` (default), `doppler`, `infisical`, `env`, `ci` or `sops`;
`--provider` takes the compute provider. Setup has no flags for other slots: you change
those in `config/stack.yaml` in phase 4. For Infisical, Setup leaves
`providers.secrets_infisical.project_id` empty for you to fill in.

### Phase 4: work the checklist

Comment **`/sealedstack check`** on PR #1. The agent reads the checklist comment, which
names for every red item *what happened, why, the fix and a link*, and sorts them:

- **Config it can fix in the PR:** your SSH public key in `admin.ssh_keys`, the tailnet
  name, the Infisical `project_id`, slot choices, app and server entries.
- **Values only you can supply:** anything that says "not found in the secret source" or
  "token is not set". The agent tells you the exact key and where it goes, then you add
  it and comment `/sealedstack check` again.

Repeat until every item is green. `check` also imports your Tailscale policy as a commit
on the Setup branch for you to review.

### Phase 5: merge, then `up`

Merge PR #1. That merge is your approval of the configuration; nothing outside the
repository has changed yet. Then run `up` (Actions, sealedstack, Run workflow, command
`up`) and approve it. If a step fails, the job summary names the step, the cause and the
fix, and **running `up` again continues** where it stopped. Have the agent read the
failure and propose the fix as a pull request.

### Phase 6: verify

```bash
curl -sI https://hello.example.com | head -1      # 302 to the Access login
curl -sI https://hello.example.com/.env | head -1 # 403
nc -zv <public-ip> 22 80 443 3000                  # all must time out
```

Then close TCP 22 at the provider firewall and delete the server IP and root password
from the secret source (see [getting started](getting-started.md#6-verify)).

### Phase 7: add an app

```bash
gh workflow run sealedstack.yml -f command=add-app \
  -f args='--name example-app --repo you/example-app --host app.example.com --access owner-only'
```

This opens a pull request in the app repo and one in the infra repo. Merge the app PR
first, then the infra PR ([journey C](user-experience.md#6-journey-c-add-an-app)). The
app's secrets go in the same secret source as `<app>.<NAME>` (for example
`shop.CLERK_SECRET_KEY`); see the naming rule for your source in
[prerequisites](prerequisites.md). Release with the app repo's `release` workflow.

### Phase 8: prove you can rebuild

Run `drill` for a database app: it restores the latest backup into a throwaway database
and reports. A setup you have never restored from is not a backup.

## A prompt to start the agent

Paste this at the start of the session, after phase 1:

```text
You are helping me set up sealedstack in this infra repo. Read docs/run-with-an-agent.md,
docs/getting-started.md, docs/prerequisites.md and docs/choosing-a-stack.md first.

Rules:
- Change things only through pull requests. Never apply, never use --break-glass.
- You never see secret values. config/ holds only secret NAMES. If something needs a
  value, tell me the exact key, where it goes, and wait for me to add it.
- I merge PR #1 and approve `up`. Do not try to do either.
- Work one phase at a time. After each, comment /sealedstack check and show me the red
  items, grouped into "you can fix in the PR" and "I must supply".
- When a run fails, read the job summary and the Status issue, quote the cause and the
  fix, and propose it as a pull request.

My stack: <golden path | list the slots you changed>. My domain: <domain>.
Start with phase 2.
```

## When something goes wrong

1. Open the failed job's **summary** and the pinned **Status issue**. Every failure is
   *what happened, why, the fix, a link*.
2. Fix it in a pull request (config) or in the secret source (a value), comment
   `/sealedstack check`, then run the same command again. `up` resumes.
3. If the fix is not obvious, check [limitations](limitations.md) and the
   troubleshooting list in [getting started](getting-started.md#troubleshooting).
4. Write down what differed from this page. The first live run is how the unproven parts
   (the provider APIs listed as open in [decisions](decisions.md)) get confirmed.
