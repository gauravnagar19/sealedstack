#!/usr/bin/env bash
# Cut a sealedstack release from the current checkout (maintainers only, D-080).
#
#   scripts/release.sh [--minor | --version X.Y.Z] [--dry-run] [--yes] [--no-wait]
#
# What it does, in order, and stops at the first failure:
#   1. needs a clean tree, HEAD rebased onto origin/main and at least one commit to ship;
#   2. picks the next version: one more than the highest of VERSION on origin/main and every vX.Y.Z tag
#      (so a release another session cut a minute ago is never reused);
#   3. writes VERSION and commits "release: X.Y.Z";
#   4. runs the tests, `tofu fmt -check` and `scripts/publish-check.sh --strict`;
#   5. fetches again and refuses if origin/main moved meanwhile;
#   6. pushes HEAD to main, pushes the lightweight tag vX.Y.Z, dispatches the release workflow and waits for it.
#
# --dry-run prints the version and the commits that would ship, changing nothing.
# --yes skips the confirmation before the push. --no-wait leaves the release run to finish on its own.
#
# Overrides (mostly for tests): RELEASE_TEST_CMD, RELEASE_FMT_CMD, RELEASE_CHECK_CMD, RELEASE_REMOTE (default origin),
# RELEASE_WORKFLOW (default release.yml).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$here"
remote="${RELEASE_REMOTE:-origin}"
workflow="${RELEASE_WORKFLOW:-release.yml}"
bump=patch version="" dry=0 yes=0 wait=1

usage() { sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; }
die() { echo "release: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --minor) bump=minor; shift ;;
    --version) version="${2:-}"; shift 2 ;;
    --dry-run) dry=1; shift ;;
    --yes|-y) yes=1; shift ;;
    --no-wait) wait=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

semver='^[0-9]+\.[0-9]+\.[0-9]+$'
[[ -z "$version" || "$version" =~ $semver ]] || die "--version must look like 1.2.3"

[[ -z "$(git status --porcelain)" ]] || die "the working tree is not clean; commit or stash first"
git fetch -q "$remote" --tags
git rev-parse -q --verify "refs/remotes/$remote/main" >/dev/null || die "no $remote/main to release against"
git merge-base --is-ancestor "$remote/main" HEAD || die "HEAD does not contain $remote/main; rebase first (git rebase $remote/main)"
[[ "$(git rev-parse HEAD)" != "$(git rev-parse "$remote/main")" ]] || die "nothing to release: HEAD is $remote/main"

# The highest version anyone has used: VERSION on main, and every vX.Y.Z tag.
highest="$(
  { git show "$remote/main:VERSION" | tr -d '[:space:]'; echo
    git tag -l 'v[0-9]*.[0-9]*.[0-9]*' | sed 's/^v//'
  } | grep -E "$semver" | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1
)"
[[ -n "$highest" ]] || die "could not work out the current version"
if [[ -z "$version" ]]; then
  IFS=. read -r major minor patch <<<"$highest"
  if [[ "$bump" == minor ]]; then version="$major.$((minor + 1)).0"; else version="$major.$minor.$((patch + 1))"; fi
fi
if git rev-parse -q --verify "refs/tags/v$version" >/dev/null || git ls-remote --exit-code --tags "$remote" "v$version" >/dev/null 2>&1; then
  die "v$version already exists"
fi

echo "release: $highest -> $version"
echo "commits that ship:"
git log --oneline "$remote/main..HEAD" | sed 's/^/  /'
(( dry == 0 )) || { echo "release: --dry-run, nothing changed"; exit 0; }

printf '%s\n' "$version" > VERSION
git add VERSION
git commit -q -m "release: $version"

if [[ -n "${RELEASE_TEST_CMD:-}" ]]; then
  bash -c "$RELEASE_TEST_CMD"
elif docker image inspect sealedstack-tests:dev >/dev/null 2>&1; then
  docker run --rm --init -v "$here":/repo -w /repo -e PYTHONPATH=/repo/engine sealedstack-tests:dev \
    python -m pytest -q -p no:cacheprovider tests/pytest
else
  PYTHONPATH="$here/engine" python3 -m pytest -q -p no:cacheprovider tests/pytest
fi

# CI runs `tofu fmt -check`; a misaligned local cost release 0.6.15 a red CI run, so run it here too.
if [[ -n "${RELEASE_FMT_CMD:-}" ]]; then
  bash -c "$RELEASE_FMT_CMD"
elif command -v tofu >/dev/null 2>&1; then
  tofu fmt -check -recursive tofu
elif docker image inspect sealedstack-toolbox:dev >/dev/null 2>&1; then
  docker run --rm -v "$here":/repo -w /repo sealedstack-toolbox:dev tofu fmt -check -recursive tofu
else
  echo "release: warning: no tofu and no sealedstack-toolbox:dev image, skipping 'tofu fmt -check'" >&2
fi
bash -c "${RELEASE_CHECK_CMD:-scripts/publish-check.sh --strict}"

git fetch -q "$remote" --tags
git merge-base --is-ancestor "$remote/main" HEAD || die "$remote/main moved while the checks ran; the release commit is kept locally. Rebase and run again."
git rev-parse -q --verify "refs/tags/v$version" >/dev/null && die "v$version was created while the checks ran"

if (( yes == 0 )); then
  read -r -p "push to $remote/main, tag v$version and start the release? [y/N] " answer
  [[ "$answer" == y || "$answer" == Y ]] || die "stopped before the push; the release commit is kept locally"
fi

git push "$remote" HEAD:main
git tag "v$version"
git push "$remote" "v$version"
gh workflow run "$workflow" -f "version=$version"
echo "release: v$version pushed and the release workflow dispatched"

if (( wait )); then
  sleep 8
  run_id="$(gh run list --workflow "$workflow" --limit 1 --json databaseId --jq '.[0].databaseId')"
  echo "release: watching run $run_id"
  gh run watch "$run_id" --exit-status
fi
