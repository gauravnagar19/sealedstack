#!/usr/bin/env bash
# Run the sealedstack test suite: bats (shell) then pytest (Python helpers).
#
# Inside the toolbox these run with bash 4+, jq, yq, tofu and the project's
# Python dependencies already present. Locally on macOS, install bats-core and
# pytest (e.g. in the toolbox) before running this.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
cd "${repo}"

# The Python engine package lives under engine/ (not pip-installed in the dev
# toolbox); put it on the path so `import sealedstack` works for the tests.
export PYTHONPATH="${repo}/engine:${PYTHONPATH:-}"

if ! command -v bats >/dev/null 2>&1; then
  echo "tests: bats not found (install bats-core, e.g. 'apt-get install bats')" >&2
  exit 1
fi
if ! python3 -c 'import pytest' >/dev/null 2>&1; then
  echo "tests: pytest not found (pip install pytest)" >&2
  exit 1
fi

status=0
echo "== bats =="
bats_log="$(mktemp)"
if ! bats -r tests/bats | tee "${bats_log}"; then
  status=1
fi
plan="$(grep -m1 '^1\.\.' "${bats_log}" || true)"
count="${plan#1..}"
if [[ -z "${plan}" || ! "${count}" =~ ^[0-9]+$ || "${count}" -eq 0 ]]; then
  echo "tests: bats ran 0 tests (no TAP plan line) — treating as failure" >&2
  status=1
fi
rm -f "${bats_log}"

echo "== pytest =="
python3 -m pytest tests/pytest -q || status=$?

exit "${status}"
