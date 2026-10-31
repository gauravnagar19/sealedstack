"""Pin the SSH host key of a new server for the first, password-authenticated login (D-064, DR-5).

The first connection to a freshly created machine goes to its public IP and sends
the root password. Nothing authenticates that path yet, so someone in between could
capture the password. With ``bootstrap.host_key_fingerprint`` in ``servers.yaml``
(the SHA256 fingerprint the provider shows in its console; it is not secret) the
bootstrap play runs with host-key checking on against a ``known_hosts`` that holds
only the key whose fingerprint matches. A server that presents another key is refused
before any password is sent.
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Iterable

from . import config as config_mod

FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
KEY_TYPES = "ed25519,ecdsa,rsa"
WHERE = (
    "the provider console shows it for the machine (Netcup: Server Control Panel, the server's details; "
    "most providers: the server or SSH page), or run `ssh-keygen -lf <(ssh-keyscan -t ed25519 <ip>)` yourself "
    "from a network you trust"
)


class HostKeyError(config_mod.ConfigError):
    pass


def normalise(fingerprint: str) -> str:
    """``SHA256:<base64>`` without padding; raises when the text is not a SHA256 fingerprint."""
    text = str(fingerprint or "").strip().rstrip("=")
    if not FINGERPRINT_RE.match(text):
        raise HostKeyError(
            f"bootstrap.host_key_fingerprint '{fingerprint}' is not a SHA256 fingerprint "
            "(expected the form SHA256:<43 base64 characters>, as providers and `ssh-keygen -l` show it)"
        )
    return text


def fingerprint(key_b64: str) -> str:
    """The OpenSSH SHA256 fingerprint of a base64 public key blob."""
    digest = hashlib.sha256(base64.b64decode(key_b64.encode("ascii"))).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def parse_scan(output: str) -> "list[tuple[str, str]]":
    """``(key type, base64 key)`` for every key line of ``ssh-keyscan`` output."""
    keys = []
    for line in str(output or "").splitlines():
        parts = line.split()
        if len(parts) >= 3 and not line.startswith("#") and parts[1].startswith(("ssh-", "ecdsa-")):
            keys.append((parts[1], parts[2]))
    return keys


def known_hosts_line(output: str, host: str, wanted: str, *, name: str = "the server") -> str:
    """A ``known_hosts`` line for ``host`` holding the scanned key that matches ``wanted``.

    Raises ``HostKeyError`` (naming the fingerprints seen, never the address) when the
    server answered with other keys or with none.
    """
    want = normalise(wanted)
    keys = parse_scan(output)
    if not keys:
        raise HostKeyError(
            f"could not read an SSH host key from {name} to check bootstrap.host_key_fingerprint; "
            "check that TCP 22 is open at the provider's firewall and that the IP in the secret source is current"
        )
    for key_type, blob in keys:
        if fingerprint(blob) == want:
            return f"{host} {key_type} {blob}\n"
    seen = ", ".join(f"{fingerprint(blob)} ({key_type.replace('ssh-', '')})" for key_type, blob in keys)
    raise HostKeyError(
        f"the SSH host key of {name} does not match bootstrap.host_key_fingerprint.\n"
        f"  Expected: {want}\n  Presented: {seen}\n"
        "Refusing to send the root password. If the fingerprint in servers.yaml is wrong, correct it "
        f"({WHERE}); if it is right, something may be intercepting the connection."
    )


def scan_command(host: str, port: int = 22) -> "list[str]":
    return ["ssh-keyscan", "-T", "10", "-p", str(port), "-t", KEY_TYPES, host]


def configured(config: config_mod.Config, name: str) -> str:
    """The pinned fingerprint of a server (``""`` when none is set)."""
    spec = (config.servers.get("servers") or {}).get(name) or {}
    return str((spec.get("bootstrap") or {}).get("host_key_fingerprint") or "")
