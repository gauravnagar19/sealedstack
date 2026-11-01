#!/usr/bin/env bash
# Run the sealedstack test suite (pytest, including the engine).
#
# Inside the toolbox these run with tofu and the project's Python dependencies
# present. Locally, install pytest (e.g. run this in the toolbox).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "${here}/.." && pwd)"
cd "${repo}"

# The Python engine package lives under engine/ (not pip-installed in the dev
# toolbox); put it on the path so `import sealedstack` works for the tests.
export PYTHONPATH="${repo}/engine:${PYTHONPATH:-}"

if ! python3 -c 'import pytest' >/dev/null 2>&1; then
  echo "tests: pytest not found (pip install pytest)" >&2
  exit 1
fi

# The suite is CPU-bound once the fake servers stopped sleeping, so spread it over the
# cores when pytest-xdist is installed (it is in the toolbox). An explicit -n wins.
parallel=()
if python3 -c 'import xdist' >/dev/null 2>&1; then
  case " $* " in
    *" -n"*) ;;
    *) parallel=(-n auto) ;;
  esac
fi

python3 -m pytest tests/pytest -q ${parallel[@]+"${parallel[@]}"} "$@"
