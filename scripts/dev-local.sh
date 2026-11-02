#!/usr/bin/env bash
# Run the engine from THIS checkout in a container against a real infra repo (maintainers only, D-071).
#
# Why: a fix normally takes a release, a pin bump and a merge before CI sees it. Here the checkout is mounted into the
# toolbox image, so an edit is live on the next run.
#
#   scripts/dev-local.sh --infra ~/work/my-infra --env ~/work/.env [--tailnet] [--docker] [--build] -- check
#   scripts/dev-local.sh --infra ... --env ... --tailnet --phase server up server-1
#   scripts/dev-local.sh --infra ... --env ... --tailnet --phase up        # the whole `up` sequence, resumable
#
# Without --phase the arguments go to the `sealedstack` CLI, with its guards: reads (check, plan, status, drift) work, and a
# write command still needs --break-glass and the checks that come with it.
# With --phase the first argument is an engine module run directly (`stateinit`, `edge apply`, `server up server-1`,
# `dokploybootstrap`, `platform apply`, ...). That skips the approval gate, the state lock and the audit post. It is for
# iterating on a test setup; never use it on infrastructure you cannot rebuild.
#
# Only the secret-source credentials and the bot token are passed to the container, read from the env file by name
# (the file is never sourced). --tailnet joins the tailnet from inside the container as an ephemeral tag:ci node, which
# phases that reach the server over Tailscale need (Docker Desktop containers cannot use the host's Tailscale).
# --docker also mounts the host's /var/run/docker.sock, for commands that start containers from the engine (the
# `backup check --where runner` drill, D-085). It gives the container control of the host's Docker; off by default.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
infra="${SEALEDSTACK_DEV_INFRA:-}"
env_file="${SEALEDSTACK_DEV_ENV:-}"
image="${SEALEDSTACK_DEV_IMAGE:-sealedstack-toolbox:dev}"
tailnet=0 build=0 phase=0 docker_sock=0

usage() { sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --infra) infra="$2"; shift 2 ;;
    --env) env_file="$2"; shift 2 ;;
    --image) image="$2"; shift 2 ;;
    --tailnet) tailnet=1; shift ;;
    --docker) docker_sock=1; shift ;;
    --build) build=1; shift ;;
    --phase) phase=1; shift ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    *) break ;;
  esac
done

[[ -n "$infra" && -d "$infra/config" ]] || { echo "dev-local: --infra must be an infra repo checkout (has config/)" >&2; exit 2; }
[[ -n "$env_file" && -f "$env_file" ]] || { echo "dev-local: --env must be a file with the secret-source credentials" >&2; exit 2; }
[[ $# -gt 0 ]] || { usage >&2; exit 2; }
command -v docker >/dev/null || { echo "dev-local: docker is required" >&2; exit 2; }
infra="$(cd "$infra" && pwd)"

if (( build )) || ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "dev-local: building the toolbox image $image" >&2
  docker build -t "$image" "$here/toolbox" >&2
fi

# Pass only these names, taken from the env file as plain text.
allow=(DOPPLER_TOKEN DOPPLER_TOKEN_APPS DOPPLER_ADMIN_TOKEN BWS_ACCESS_TOKEN INFISICAL_CLIENT_ID INFISICAL_CLIENT_SECRET INFISICAL_TOKEN GH_TOKEN)
(( tailnet )) && allow+=(TAILSCALE_CI_OAUTH_CLIENT_ID TAILSCALE_CI_OAUTH_CLIENT_SECRET)

umask 077
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT
for name in "${allow[@]}"; do
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$env_file" | tail -1 || true)"
  [[ -n "$line" ]] || continue
  value="${line#*=}"
  value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
  printf '%s=%s\n' "$name" "$value" >> "$tmp"
done

commit="$(git -C "$infra" rev-parse HEAD 2>/dev/null || true)"
# The engine renders into <code>/.build and keeps run progress in <code>/.sealedstack. The test suite renders the EXAMPLE
# config into the same folders, which once made an edge plan target example.com. Keep this run's own, per infra repo.
cache="${SEALEDSTACK_DEV_CACHE:-$HOME/.cache/sealedstack-dev}/$(basename "$infra")"
mkdir -p "$cache/build" "$cache/workdir"
args=(run --rm -v "$here:/opt/sealedstack" -v "$cache/build:/opt/sealedstack/.build" -v "$cache/workdir:/opt/sealedstack/.sealedstack"
  -v "$infra:/infra" -w /infra --env-file "$tmp"
  -e PYTHONPATH=/opt/sealedstack/engine -e SEALEDSTACK_CODE=/opt/sealedstack -e SEALEDSTACK_CONFIG=/infra/config
  -e SEALEDSTACK_COMMIT="$commit"
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0='*')
[[ -t 0 && -t 1 ]] && args+=(-it) || args+=(-i)
if (( docker_sock )); then
  [[ -S /var/run/docker.sock ]] || { echo "dev-local: --docker needs /var/run/docker.sock on this host" >&2; exit 2; }
  args+=(-v /var/run/docker.sock:/var/run/docker.sock)
fi

if (( phase )); then
  module="$1"; shift
  case "$module" in
    up|apply|plan)
      # The same phase list CI runs (with its saved progress), without the approval gate in front of it.
      engine=(python3 -c 'import os, sys
from pathlib import Path
from sealedstack import cli, config as cfg
code, env = Path(os.environ["SEALEDSTACK_CODE"]), dict(os.environ)
config = cfg.load_config(cfg.resolve_config_dir(None, env, code), code, env)
sys.exit(cli._orchestrate_handler(sys.argv[1])(config, env, code, False, cli.default_runner))' "$module") ;;
    *.py) engine=(python3 "/opt/sealedstack/scripts/${module}" "$@") ;;   # a maintainers' script from scripts/
    *) engine=(python3 -m "sealedstack.${module}" "$@") ;;
  esac
else
  engine=(python3 -m sealedstack "$@")
fi

if (( tailnet )); then
  args+=(--cap-add NET_ADMIN --device /dev/net/tun --entrypoint bash)
  exec docker "${args[@]}" "$image" /opt/sealedstack/scripts/dev-tailnet-entry.sh "${engine[@]}"
fi
exec docker "${args[@]}" --entrypoint "${engine[0]}" "$image" "${engine[@]:1}"
