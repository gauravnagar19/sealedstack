# Reference app

A tiny, runnable app that exercises the whole app-side flow of sealedstack: a
`sealedstack.yaml` manifest, a compose file that follows the platform rules, a
Dockerfile, and release CI. It is what CI builds and validates on every change.

| File | What it shows |
|---|---|
| `app/server.py` | One HTTP service. `GET /api/health` answers 200; `DATABASE_URL`, `REDIS_URL` and `EXAMPLE_SECRET` are optional and never printed |
| `Dockerfile` | A small non-root image |
| `docker-compose.yml` | What is deployed: no `ports:`, `build:` plus a pinned `image:` (D-024, D-030) |
| `docker-compose.override.yml` | Local development only: publishes port 3000 and defaults `APP_VERSION`; sealedstack ignores it |
| `sealedstack.yaml` | The portable manifest: web service, health path, Postgres with 6-hourly backups, Redis, one secret |
| `.github/workflows/release.yml` | Build on merge, manual release (a thin call of the reusable workflow) |

## Run it locally

```sh
docker compose up --build        # Docker Compose 2.24 or newer
curl -s localhost:3000/api/health
```

## Try the onboarding flow

`sealedstack add-app` can turn a plain app repo (with `ports:` and a database
service in its compose file) into this shape through a pull request. The
generator is covered by golden-snapshot tests (`tests/golden/onboarding`).
