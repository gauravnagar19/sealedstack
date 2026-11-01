#!/usr/bin/env bash
# Scan the whole git history for secrets with gitleaks, and fail closed.
#
#   scripts/gitleaks-history.sh [repo-dir]
#
# `gitleaks detect` exits 0 ("no leaks found") even when it could not read the
# repository at all (git's "dubious ownership" refusal for a checkout mounted into
# a container as another user did exactly that), so a green job proved nothing.
# This wrapper trusts the checkout for git, then requires that gitleaks actually
# scanned every commit before it believes "no leaks". publish-check --strict scans
# `git archive HEAD` only, not history.
set -euo pipefail

dir="$(cd "${1:-.}" && pwd)"
cd "${dir}"

# Trust this directory for git without touching anyone's global git config.
export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=safe.directory GIT_CONFIG_VALUE_0="${dir}"

expected="$(git rev-list --count --all)"
[[ "${expected}" -gt 0 ]] || { echo "gitleaks-history: no commits to scan in ${dir}" >&2; exit 1; }

rc=0
out="$(gitleaks detect --source . --redact -v --no-banner --no-color 2>&1)" || rc=$?
printf '%s\n' "${out}"

if [[ "${rc}" -ne 0 ]]; then
  echo "gitleaks-history: gitleaks exited ${rc}" >&2
  exit 1
fi
if grep -qE 'failed to scan|ERR ' <<<"${out}"; then
  echo "gitleaks-history: gitleaks reported an error, so nothing can be said about leaks" >&2
  exit 1
fi
scanned="$(grep -oE '[0-9]+ commits? scanned' <<<"${out}" | grep -oE '^[0-9]+' | tail -n 1 || true)"
if [[ "${scanned:-}" != "${expected}" ]]; then
  echo "gitleaks-history: scanned '${scanned:-none}' commits but the repository has ${expected}" >&2
  echo "gitleaks-history: is the checkout shallow? use fetch-depth: 0" >&2
  exit 1
fi
echo "gitleaks-history: ${scanned} commits scanned, no leaks"
