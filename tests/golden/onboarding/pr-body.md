This PR prepares example-app for sealedstack. Review each change:

1. `sealedstack.yaml` (new): web = ui:80, health /api/health; needs Postgres 16 with 6-hourly backups
2. `docker-compose.yml`:
   - removed `ports:` from `ui`, `api` (traffic arrives through the tunnel; published ports would be exposed)
   - removed the `postgres` service (sealedstack runs a managed, backed-up Postgres and injects `DATABASE_URL`)
   - `api`: `DATABASE_URL` is now provided by sealedstack (`${DATABASE_URL}`)
   - added `image: ghcr.io/example-owner/example-app-ui:${APP_VERSION}`, `ghcr.io/example-owner/example-app-api:${APP_VERSION}` (`build:` kept for local development)
3. `docker-compose.override.yml` (new): local development only; it defaults `APP_VERSION`, restores the ports and the database for `docker compose up`, and sealedstack ignores it
4. `.github/workflows/release.yml` (new): build on merge; manual Release; replaces `deploy.yml`, `scripts/deploy.sh` (deleted in this PR)

Things to check:
- only `ui` is routed from the internet; `api` published ports before and is now reachable by the other services only

Local development is unchanged: `docker compose up --build` still works (`docker-compose.override.yml` supplies what was removed).

**Merge this PR first.** The infra-repo PR that places this app waits for it.
