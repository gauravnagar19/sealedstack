"""Adapter for the managed OIDC gates (registry gate: okta, auth0).

Okta and Auth0 are SaaS IdPs: there is no self-hosted component. This reads
``config/providers/gate_<id>.yaml``, validates the OIDC fields and prints the
forward-auth wiring the edge gate consumes. With ``--check`` it also fetches the
OIDC discovery document and verifies the issuer. Replaces ``scripts/gate-oidc.sh``.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, Optional

import yaml

PROVIDERS = ("okta", "auth0")


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _issuer(provider: str, spec: dict) -> str:
    issuer = str(spec.get("issuer") or "")
    if issuer:
        return issuer
    if provider == "okta":
        return f"{str(spec.get('org_url') or '').rstrip('/')}/oauth2/default"
    if provider == "auth0":
        domain = str(spec.get("domain") or "")
        return f"https://{domain.removeprefix('https://')}/"
    return ""


def run(provider: str, check: bool = False, *, env: Optional[Mapping[str, str]] = None, out=None, http=None) -> int:
    env = dict(env or os.environ)
    out = out if out is not None else sys.stdout
    if provider not in PROVIDERS:
        print("usage: gate-oidc <okta|auth0> [--check]", file=sys.stderr)
        return 2
    config_dir = Path(env.get("SEALEDSTACK_CONFIG") or "config")
    spec_path = Path(env.get("SPEC_PATH") or config_dir / "providers" / f"gate_{provider}.yaml")
    if not spec_path.exists():
        print(f"gate-oidc: {spec_path} not found", file=sys.stderr)
        print(f"  copy config/providers/gate_{provider}.example.yaml to {spec_path} and fill it in", file=sys.stderr)
        return 1

    spec = _load(spec_path)
    client_id = str(spec.get("client_id") or "")
    gate_hostname = str(spec.get("gate_hostname") or "")
    scopes = " ".join(spec.get("scopes") or ["openid", "profile", "email"])
    issuer = _issuer(provider, spec)

    missing = [name for name, value in (("issuer", issuer), ("client_id", client_id), ("gate_hostname", gate_hostname)) if not value]
    if missing:
        print(f"gate-oidc: {spec_path}: missing: {', '.join(missing)}", file=sys.stderr)
        return 1

    redirect_url = f"https://{gate_hostname}/oauth2/callback"
    secret_env = f"{provider.upper()}_CLIENT_SECRET"
    print(
        f"""gate-oidc: {provider}
issuer:       {issuer}
client_id:    {client_id}
redirect_url: {redirect_url}
scopes:       {scopes}

forward-auth wiring:
  oauth2-proxy:
    --provider=oidc
    --oidc-issuer-url={issuer}
    --client-id={client_id}
    --client-secret=<from {secret_env}>
    --redirect-url={redirect_url}
    --scope="{scopes}"
    --email-domain=*
""",
        file=out,
    )

    if check:
        discovery = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        print(f"check: {discovery}", file=out)
        try:
            with urllib.request.urlopen(discovery, timeout=30) as response:
                doc = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            print(f"gate-oidc: discovery request failed: {exc}", file=sys.stderr)
            return 1
        got = str(doc.get("issuer") or "")
        if got.rstrip("/") != issuer.rstrip("/"):
            print(f"gate-oidc: issuer mismatch: config {issuer} != provider {got}", file=sys.stderr)
            return 1
        print("check: ok (issuer matches)", file=out)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    provider = args[0] if args else ""
    check = "--check" in args
    return run(provider, check)


if __name__ == "__main__":
    raise SystemExit(main())
