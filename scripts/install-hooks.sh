#!/usr/bin/env bash
# Install the repo's git hooks (fixes round 3, §A7).
#
#   scripts/install-hooks.sh
#
# Symlinks every hook in scripts/hooks/ into .git/hooks/, so edits are picked up
# without reinstalling. Repos that commit to sealedstack should run this once
# after cloning; see CONTRIBUTING.md.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
src="${repo}/scripts/hooks"
dst="${repo}/.git/hooks"

if [[ ! -d "${repo}/.git" ]]; then
  echo "install-hooks: ${repo} is not a git repository" >&2
  exit 1
fi
mkdir -p "${dst}"

shopt -s nullglob
installed=()
for hook in "${src}"/*; do
  name="$(basename "${hook}")"
  chmod +x "${hook}"
  ln -sf "../../scripts/hooks/${name}" "${dst}/${name}"
  installed+=("${name}")
done

if [[ "${#installed[@]}" -eq 0 ]]; then
  echo "install-hooks: no hooks in ${src}" >&2
  exit 0
fi
echo "install-hooks: installed ${installed[*]} into ${dst}"
