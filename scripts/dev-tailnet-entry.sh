#!/usr/bin/env bash
# Runs INSIDE the container for `dev-local.sh --tailnet` (D-071): join the tailnet as an ephemeral tag:ci node,
# then run the engine command given as arguments. The node disappears from the tailnet when the container stops.
set -euo pipefail

ver="${TAILSCALE_VERSION:-1.82.0}"
case "$(uname -m)" in
  x86_64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "dev-tailnet: unsupported architecture $(uname -m)" >&2; exit 2 ;;
esac

work="$(mktemp -d)"
url="https://pkgs.tailscale.com/stable/tailscale_${ver}_${arch}.tgz"
curl -fsSL -o "$work/ts.tgz" "$url"
want="$(curl -fsSL "${url}.sha256" | awk '{print $1}')"
have="$(sha256sum "$work/ts.tgz" | awk '{print $1}')"
[[ -n "$want" && "$want" == "$have" ]] || { echo "dev-tailnet: Tailscale download failed its checksum" >&2; exit 2; }
tar -xzf "$work/ts.tgz" -C "$work" --strip-components=1
export PATH="$work:$PATH"

mkdir -p /run/tailscale
tailscaled --state=mem: --socket=/run/tailscale/tailscaled.sock >"$work/tailscaled.log" 2>&1 &
for _ in $(seq 1 30); do [[ -S /run/tailscale/tailscaled.sock ]] && break; sleep 1; done

key="$(TAILSCALE_KEY_TAGS=tag:ci TAILSCALE_KEY_EPHEMERAL=true python3 -m sealedstack.tailscalekey)"
tailscale up --authkey="$key" --hostname="sealedstack-dev" --accept-dns=true --advertise-tags=tag:ci --timeout=60s >/dev/null
unset key
echo "dev-tailnet: joined the tailnet as sealedstack-dev (tag:ci)" >&2

set +e
"$@"
rc=$?
tailscale logout >/dev/null 2>&1 || true
exit $rc
