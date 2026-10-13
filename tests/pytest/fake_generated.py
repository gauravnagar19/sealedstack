"""An in-memory stand-in for the `generated` OpenTofu stack (same behaviour, no tofu)."""
from __future__ import annotations

import json

from sealedstack import generated as generated_mod


class MemoryBackend:
    def __init__(self):
        self.applied = []
        self.inits = 0
        self._outputs = {}

    def init(self):
        self.inits += 1

    def outputs(self):
        return json.loads(json.dumps(self._outputs))

    def apply(self, values):
        self.applied.append(dict(values))
        if "dokploy_admin_password" not in self._outputs:  # the stack creates these once
            self._outputs.update(
                dokploy_admin_password="example-admin-password-0123456789",
                dokploy_ssh_public_key="ssh-ed25519 AAAA-dokploy sealedstack-dokploy",
                dokploy_ssh_private_key="fake-private-key-dokploy\n",
                ci_ssh_public_key="ssh-ed25519 AAAA-ci sealedstack-ci",
                ci_ssh_private_key="fake-private-key-ci\n",
            )
        self._outputs["values"] = dict(values)


def fake_generated(values=None, *, seeded=True):
    backend = MemoryBackend()
    gen = generated_mod.Generated(backend)
    if seeded:
        backend.apply(values or {})
    return gen
