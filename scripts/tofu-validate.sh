#!/usr/bin/env bash
# `tofu init -backend=false` and `tofu validate` for every stack (run in the toolbox, which pins tofu).
#
#   scripts/tofu-validate.sh [stack ...]      (default: all five)
#
# Init runs one stack after another: the stacks share providers, so with
# TF_PLUGIN_CACHE_DIR set (CI caches it) each provider is downloaded once, and
# concurrent inits could write the same cache entry. Validate only reads the
# initialised directory, so those run side by side. The encryption block needs a
# value even for -backend=false (H8): TF_VAR_state_passphrase must be set.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
stacks=("$@")
[[ ${#stacks[@]} -gt 0 ]] || stacks=(state generated edge platform compute)

if [[ -n "${TF_PLUGIN_CACHE_DIR:-}" ]]; then
  mkdir -p "${TF_PLUGIN_CACHE_DIR}"
fi

for stack in "${stacks[@]}"; do
  echo "== tofu init: ${stack}"
  tofu -chdir="${root}/tofu/stacks/${stack}" init -backend=false -input=false -no-color >/dev/null
done

logs="$(mktemp -d)"
trap 'rm -rf "${logs}"' EXIT
pids=()
for stack in "${stacks[@]}"; do
  tofu -chdir="${root}/tofu/stacks/${stack}" validate -no-color >"${logs}/${stack}.log" 2>&1 &
  pids+=("$!")
done

status=0
for i in "${!stacks[@]}"; do
  if wait "${pids[$i]}"; then
    echo "== tofu validate: ${stacks[$i]}: ok"
  else
    status=1
    echo "== tofu validate: ${stacks[$i]}: FAILED"
  fi
  cat "${logs}/${stacks[$i]}.log"
done
exit "${status}"
