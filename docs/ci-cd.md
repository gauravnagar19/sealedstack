# CI/CD (the Release layer)

> **Build modes (spec §4, D-030).** The default is **build in CI on merge, promote
> on release**: images are built once per version on push to `main` and pushed to a
> registry; a release deploys an existing image with no rebuild. App repos call the
> reusable workflow `.github/workflows/app.yml`; see
> [examples/app-template/.github/workflows/release.yml](../examples/app-template/.github/workflows/release.yml)
> and the `build` block in `sealedstack.yaml`. `build.mode: server` (Dokploy builds
> on the host) is the opt-in alternative. Ownership of Dokploy fields is one-writer
> per field (spec §5, D-032). Full detail: [spec.md](spec.md).

The **Release** layer ([spec.md §2](spec.md#2-architecture-golden-path)) is the
app repo's own CI. sealedstack builds the place (a Dokploy compose service);
the app's CI ships the version. This is **D-015**
([decisions.md](decisions.md)):
sealedstack creates services with **auto-deploy off**, and each app repo's
pipeline deploys by calling the Dokploy API.

In the registry this is the `ci` slot: `registry/ci.yaml`, listed by
`sealedstack debug providers ci`. `github-actions` is the default and the only implemented
implementation; the rest are registered as `planned`. It is an optional slot in
`config/stack.yaml` (`ci: github-actions`).

```
git tag v1.2.3 && git push --tags
        │
        ▼
app CI (GitHub Actions, GitLab, …)
        │  joins the tailnet as tag:ci
        ▼
tailscale0 ──► <control-server>:3000   (Dokploy API; with 22 for SSH, the only ports tag:ci may reach)
        │
        ▼
POST /api/compose.deploy { composeId }
        │
        ▼
Dokploy pulls the image and restarts the compose service
```

Nothing about the release touches the public internet: the CI runner dials
**out** to the Tailscale control plane, gets a `100.x.y.z` address for the
control server, and calls the Dokploy API over that private interface. The
server keeps zero inbound ports ([spec.md §13](spec.md#13-security-model)).

## 1. How the release flow works

1. You push a tag (the examples use `v*`).
2. The app repo's CI job joins the Tailscale tailnet as `tag:ci`. The node is
   **ephemeral**, so Tailscale removes it when the job ends.
3. The job `POST`s to the Dokploy API at
   `http://<control-server>:3000/api/compose.deploy` with the app's compose id.
   The Tailscale ACL grant (below) is what makes port 3000 reachable.
4. Dokploy pulls the pinned image and restarts the service. Traefik and the
   Cloudflare tunnel keep serving; only the app container changes.

The Dokploy compose service is created by the platform stack
([spec.md §9](spec.md#9-platform-opentofu-stacksplatform)) with `auto-deploy`
off. The CI only triggers the deploy of the **existing** compose service.

## 2. Tailscale for CI: an OAuth client and `tag:ci`

The job must reach the Dokploy panel, and only that. The edge stack already
owns the whole ACL ([D-014](decisions.md),
`tofu/stacks/edge/main.tf`, `locals.tailscale_policy`): `tag:ci` is owned by `autogroup:admin`, and
the one grant CI gets is:

```json
{ action: "accept", src: ["tag:ci"], dst: ["tag:server:22", "tag:server:3000"] },
```

So `tag:ci` can reach ports 22 and 3000 on a server and nothing else (D-041:
port 22 is for the infra repo's own jobs, which bootstrap and sync servers over
SSH with an authorised key; port 3000 is the Dokploy API). The policy tests also
assert that `tag:ci` is denied everything else, for example the database port
5432. Add no other grants for CI.

### The OAuth client

Create a **trust credential** (OAuth client) rather than a long-lived auth key,
so there is no key to leak or rotate. In the Tailscale admin console:
**Settings → Trust credentials → Credential → OAuth**
(<https://console.tailscale.com/admin/settings/trust-credentials>).

| Setting | Value | Why |
|---|---|---|
| Scopes | `auth_keys` (**write**) | The runner mints an ephemeral node key at job time. |
| Tags | `tag:ci` | A trust credential is not tied to a user; it must tag the nodes it creates. |
| Client ID / secret | `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET` | Stored only in the CI provider's encrypted secrets. |

The tags you pass at job time must match the credential's tags, or be tags
owned by them. This project uses a single tag, `tag:ci`, which the policy
already declares in `tagOwners`. Only an Owner or Admin of the tailnet can
create a credential with that tag.

> `tailscale/github-action@v3` is used in the examples (matching
> `examples/app-template/.github/workflows/release.yml`). v4 exists; the inputs
> below are unchanged.

### ACL: what not to do

Do not add `tag:ci` to the admin rule (`autogroup:admin` already covers admins),
do not let `tag:ci` reach `:22` or `:*`, and do not use a reusable auth key
committed anywhere. The single `:3000` grant is the whole surface.

## 3. Joining from GitHub Actions

Use the official action. It reads the OAuth client, creates an ephemeral
`tag:ci` node and logs out at the end of the job.

```yaml
- name: Join the tailnet as tag:ci
  uses: tailscale/github-action@v3
  with:
    oauth-client-id: ${{ secrets.TS_OAUTH_CLIENT_ID }}
    oauth-secret: ${{ secrets.TS_OAUTH_SECRET }}
    tags: tag:ci

- name: Trigger the Dokploy deployment
  run: |
    curl -fsS -X POST \
      -H "x-api-key: ${{ secrets.DOKPLOY_API_KEY }}" \
      -H "Content-Type: application/json" \
      -d "{\"composeId\":\"${{ secrets.DOKPLOY_APP_ID }}\"}" \
      "${{ secrets.DOKPLOY_URL }}/api/compose.deploy"
```

The complete workflow is
[`examples/app-template/.github/workflows/release.yml`](../examples/app-template/.github/workflows/release.yml).
Set the repository secrets in **Settings → Secrets and variables → Actions**.
`DOKPLOY_URL` and `DOKPLOY_API_KEY` are the same for every app and can live at
the organisation level.

## 4. Joining from GitLab CI

GitLab has two options; both end with the same `curl`.

**Auth key in a CI variable (simplest).** Mint an ephemeral, tagged auth key and
store it as a masked, protected variable `TS_AUTHKEY`:

```yaml
deploy:
  stage: deploy
  rules:
    - if: $CI_COMMIT_TAG =~ /^v/
  image: tailscale/tailscale:latest
  variables:
    TS_STATE_DIR: /tmp/tailscale-state
    TS_SOCKET: /tmp/tailscaled.sock
  script:
    - tailscaled --state=$TS_STATE_DIR/state --socket=$TS_SOCKET --tun=userspace-networking &
    - sleep 3
    - tailscale --socket=$TS_SOCKET up --authkey="$TS_AUTHKEY"
        --hostname="ci-$CI_PROJECT_NAME-$CI_PIPELINE_IID" --advertise-tags=tag:ci
    - tailscale --socket=$TS_SOCKET status
    - |
      curl -fsS -X POST \
        -H "x-api-key: $DOKPLOY_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"composeId\":\"$DOKPLOY_APP_ID\"}" \
        "$DOKPLOY_URL/api/compose.deploy"
```

`--tun=userspace-networking` lets the job run in an unprivileged container — no
`/dev/net/tun`, no `NET_ADMIN`. The full file is
[`examples/app-template/.gitlab-ci.yml`](../examples/app-template/.gitlab-ci.yml).
Store `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY` and `DOKPLOY_APP_ID` as
CI/CD variables (**Settings → CI/CD → Variables**), marked *masked*.

**OIDC instead of an auth key.** Tailscale workload identity federation lets the
job exchange GitLab's OIDC token for a node key, so no Tailscale secret is
stored in GitLab at all. Create an OIDC federated credential with the
`auth_keys` write scope and `tag:ci`, then authenticate the runner with it. See
<https://tailscale.com/kb/1581/workload-identity-federation>.

## 5. Joining from Gitea Actions

Gitea Actions understands the GitHub Actions syntax; an `act_runner` job can use
the same Tailscale action if it can fetch public actions, otherwise install the
client and use an auth key:

```yaml
steps:
  - uses: actions/checkout@v4
  - name: Join the tailnet as tag:ci
    run: |
      curl -fsSL https://tailscale.com/install.sh | sh
      tailscaled --state=/tmp/ts/state --socket=/tmp/ts/sock --tun=userspace-networking &
      sleep 3
      tailscale --socket=/tmp/ts/sock up --authkey="${{ secrets.TS_AUTHKEY }}" \
        --hostname="ci-${{ github.run_id }}" --advertise-tags=tag:ci
  - name: Deploy
    run: |
      curl -fsS -X POST \
        -H "x-api-key: ${{ secrets.DOKPLOY_API_KEY }}" \
        -H "Content-Type: application/json" \
        -d "{\"composeId\":\"${{ secrets.DOKPLOY_APP_ID }}\"}" \
        "${{ secrets.DOKPLOY_URL }}/api/compose.deploy"
```

Set `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` as repository
secrets (**Settings → Actions → Secrets**). See
<https://docs.gitea.com/usage/actions/overview>.

## 6. Joining from a generic runner

Any runner that can run the Tailscale client on Linux (or macOS) works:

```sh
curl -fsSL https://tailscale.com/install.sh | sh

# Unprivileged container? add --tun=userspace-networking.
sudo tailscaled --state=/tmp/ts/state --socket=/tmp/ts/sock &
sleep 3
sudo tailscale --socket=/tmp/ts/sock up \
  --authkey="$TS_AUTHKEY" \
  --hostname="ci-$(date +%s)" \
  --advertise-tags=tag:ci
sudo tailscale --socket=/tmp/ts/sock status

curl -fsS -X POST \
  -H "x-api-key: $DOKPLOY_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"composeId\":\"$DOKPLOY_APP_ID\"}" \
  "$DOKPLOY_URL/api/compose.deploy"
```

Mint `TS_AUTHKEY` as an **ephemeral, pre-authorized auth key tagged `tag:ci`**
(Tailscale admin console → Settings → Keys, or an OAuth client with the
`auth_keys` scope). Ephemeral means the node disappears when the job exits.
Never commit the key; pass it as a CI secret.

Prefer the OAuth client where the provider supports OIDC, so the runner holds no
long-lived Tailscale secret.

## 7. Publish the compose id (`DOKPLOY_APP_ID`)

> **Automatic with sealedstack.** `apply` and `up` publish these settings to each
> app repo for you, through the forge adapter: the variables
> `SEALEDSTACK_COMPOSE_IDS`, `SEALEDSTACK_DOKPLOY_URL`,
> `SEALEDSTACK_TS_OAUTH_CLIENT_ID`, `SEALEDSTACK_TS_AUDIENCE` and
> `SEALEDSTACK_REGISTRY`, and the secret `SEALEDSTACK_DOKPLOY_API_KEY` in the
> `production` environment (`sealedstack debug publish` re-runs it). The manual
> steps below are for CI systems the adapter does not cover yet.

The platform stack creates the app's compose service and exposes its id. After
`sealedstack apply` (the CI run after you merge):

```sh
tofu -chdir=tofu/stacks/platform output -json compose_ids
# {"shop": {"server-1": "a1b2c3..."}}
```

Take the id for the app and store it, together with the panel URL and API key,
as secrets in the **app repo**:

```sh
gh secret set DOKPLOY_APP_ID --repo you/shop --body "a1b2c3..."
gh secret set DOKPLOY_URL    --repo you/shop --body "http://server-1:3000"
gh secret set DOKPLOY_API_KEY --repo you/shop --body "<from Bitwarden>"
```

- `DOKPLOY_URL` is the control server over MagicDNS, always `http://<control-server>:3000`.
- `DOKPLOY_API_KEY` lives in Bitwarden `infra` (`DOKPLOY_API_KEY`); it is created
  by Dokploy on first boot.
- `DOKPLOY_APP_ID` is the `compose_ids[<app>][<server>]` value above. On
  `apply` it can change; re-publish it then (v0.2 automates this).

`TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` (or `TS_AUTHKEY`) are per tailnet, not
per app, so they can be org-wide or duplicated per repo.

## 8. Provider credentials at a glance

| Provider | Join method | Secrets / variables | Docs |
|---|---|---|---|
| GitHub Actions | `tailscale/github-action@v3` | `TS_OAUTH_CLIENT_ID`, `TS_OAUTH_SECRET`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://docs.github.com/actions> |
| GitLab CI/CD | Tailscale image + auth key, or OIDC | `TS_AUTHKEY` (or OIDC), `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://docs.gitlab.com/ee/ci/> |
| Gitea Actions | Tailscale client (auth key), or the GitHub action | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://docs.gitea.com/usage/actions/overview> |
| Woodpecker CI | Tailscale client (auth key) | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://woodpecker-ci.org/docs/intro> |
| Drone CI | Tailscale client (auth key) | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://docs.drone.io/> |
| Jenkins | Tailscale client (auth key) in a pipeline step | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://www.jenkins.io/doc/book/pipeline/> |
| Buildkite | Tailscale client (auth key) on the agent | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://buildkite.com/docs/pipelines> |
| CircleCI | Tailscale client (auth key), usually userspace | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://circleci.com/docs/> |
| Bitbucket Pipelines | Tailscale client (auth key) | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://support.atlassian.com/bitbucket-cloud/docs/configure-bitbucket-pipelinesyml/> |
| AWS CodeBuild | Tailscale client (auth key) in `buildspec.yml` | `TS_AUTHKEY`, `DOKPLOY_URL`, `DOKPLOY_API_KEY`, `DOKPLOY_APP_ID` | <https://docs.aws.amazon.com/codebuild/latest/userguide/welcome.html> |

The `ci` registry entries (`registry/ci.yaml`) record the same secret names, so
`sealedstack debug providers ci` and the registry stay the source of truth.

## Related

- Selection: set `slots.ci` in `config/stack.yaml`
  ([stack.example.yaml](../config/stack.example.yaml)).
- Registry: [`registry/ci.yaml`](../registry/ci.yaml) ·
  [providers.md](providers.md).
- Interchangeable providers are catalogued per slot in
  [providers.md](providers.md); adding one is described in
  [AGENTS.md](../AGENTS.md).
