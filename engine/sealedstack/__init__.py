"""sealedstack engine (M6).

The engine is the container image's CLI: it validates, renders and drives the
OpenTofu/Ansible stacks from a small, secret-free config. This package is the
Python orchestration layer (D-047) that replaces the former bash dispatcher
(``scripts/sealedstack``).
"""

__version__ = "0.5.0"

__all__ = ["__version__"]
