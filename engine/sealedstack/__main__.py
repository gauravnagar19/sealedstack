"""Allow ``python3 -m sealedstack`` as an alias for the console script."""
from __future__ import annotations

from .cli import main

if __name__ == "__main__":  # pragma: no cover - exercised via subprocess
    raise SystemExit(main())
