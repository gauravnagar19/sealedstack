"""Preflight ``check`` (spec 7): read-only, explains every failure.

Every check is a pure function of an injected :class:`CheckContext`, so the whole
suite is unit testable with mocked HTTP, secrets, git and TCP. The config area
deliberately *reuses* ``scripts/validate.py`` and ``scripts/render.py`` instead of
reimplementing the schemas, cross-file rules or compose linting.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
import socket
import sys
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import config as cfg
from .http import HttpClient, UrllibClient
from .secrets import SecretSource, build_source

STATUS_ICON = {"ok": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏳"}
STATUS_ORDER = ("fail", "warn", "ok", "skip")

DOCS_CF = "docs/prerequisites.md#cloudflare"
DOCS_TS = "docs/prerequisites.md#tailscale"
DOCS_FORGE = "docs/infra-repo.md"
DOCS_CONFIG = "docs/getting-started.md"
DOCS_SECRETS = "docs/prerequisites.md"
DOCS_BREAK_GLASS = "docs/user-experience.md"

CF_API = "https://api.cloudflare.com/client/v4"
TS_API = "https://api.tailscale.com/api/v2"
GITHUB_API = "https://api.github.com"
GITLAB_API = "https://gitlab.com/api/v4"

# Placeholder version policy data (spec 6.7 / 7). A real advisory feed can
# replace this without changing the check's shape.
DEPRECATED_BELOW = "0.4.0"
AFFECTED_VERSIONS: dict[str, str] = {}
NEWEST_KNOWN_VERSION: Optional[str] = None

# A harmless API call per Cloudflare permission required by the golden path
# (spec 6.1). ``{zone_id}``/``{account_id}`` are filled from stack.yaml.
CF_PERMISSIONS: list[tuple[str, str, str]] = [
    ("dns_edit", "Zone · DNS: Edit", "/zones/{zone_id}/dns_records?per_page=1"),
    ("tunnel_edit", "Account · Cloudflare Tunnel: Edit", "/accounts/{account_id}/cfd_tunnel?per_page=1"),
    ("access_edit", "Account · Access: Apps and Policies: Edit", "/accounts/{account_id}/access/apps?per_page=1"),
    ("r2_edit", "Account · Workers R2 Storage: Edit", "/accounts/{account_id}/r2/buckets"),
    ("waf_edit", "Zone · WAF: Edit", "/zones/{zone_id}/rulesets?per_page=1"),
    ("bot_edit", "Zone · Bot Management: Edit", "/zones/{zone_id}/bot_management"),
    ("redirect_edit", "Zone · Single Redirect: Edit", "/zones/{zone_id}/rulesets/phases/http_request_dynamic_redirect/entrypoint"),
]


@dataclass
class Result:
    id: str
    area: str
    status: str
    detail: str = ""
    fix: str = ""
    docs: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CheckContext:
    config: cfg.Config
    env: Mapping[str, str]
    http: HttpClient
    secrets: SecretSource
    registry: dict
    version: str
    build_dir: Path
    tcp_probe: Callable[[str, int, float], bool] = field(
        default=lambda host, port, timeout: False
    )


def default_tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _resolve_build_dir(config_dir: Path, code_root: Path, env: Mapping[str, str]) -> Path:
    workdir = env.get("SEALEDSTACK_WORKDIR")
    if workdir:
        return Path(workdir) / ".build"
    candidate = code_root / ".build"
    if os.access(str(code_root), os.W_OK):
        return candidate
    import tempfile

    return Path(tempfile.mkdtemp(prefix="sealedstack-build-"))


def _load_script_modules(code_root: Path) -> tuple[Any, Any, Any, Any]:
    scripts = str(Path(code_root) / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import lint_compose  # type: ignore
    import registry as registry_mod  # type: ignore
    import render as render_mod  # type: ignore
    import validate as validate_mod  # type: ignore

    return validate_mod, registry_mod, render_mod, lint_compose


def build_context(
    config: cfg.Config,
    *,
    env: Optional[Mapping[str, str]] = None,
    http: Optional[HttpClient] = None,
    secrets: Optional[SecretSource] = None,
    registry: Optional[dict] = None,
    version: Optional[str] = None,
    build_dir: Optional[Path] = None,
    tcp_probe: Optional[Callable[[str, int, float], bool]] = None,
    bw_runner: Optional[Callable] = None,
    sops_runner: Optional[Callable] = None,
) -> CheckContext:
    env = dict(os.environ) if env is None else dict(env)
    if registry is None:
        try:
            _, registry_mod, _, _ = _load_script_modules(config.code_root)
            registry = registry_mod.load()
        except Exception:  # noqa: BLE001 - offline/import failure surfaces as a check
            registry = {}
    return CheckContext(
        config=config,
        env=env,
        http=http or UrllibClient(),
        secrets=secrets or build_source(config.stack, config.config_dir, env, bw_runner=bw_runner, sops_runner=sops_runner),
        registry=registry,
        version=version if version is not None else cfg.read_version(config.code_root),
        build_dir=Path(build_dir) if build_dir else _resolve_build_dir(config.config_dir, config.code_root, env),
        tcp_probe=tcp_probe or default_tcp_probe,
    )


def _result(result_id: str, area: str, problems: list[str], *, warn: bool = False, fix: str = "", docs: str = "") -> Result:
    if problems:
        return Result(result_id, area, "warn" if warn else "fail", detail="; ".join(problems), fix=fix, docs=docs)
    return Result(result_id, area, "ok", detail="valid", docs=docs)


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def check_config(ctx: CheckContext) -> list[Result]:
    try:
        validate, registry_mod, render_mod, lint = _load_script_modules(ctx.config.code_root)
    except Exception as exc:  # noqa: BLE001
        return [
            Result(
                "config.import",
                "config",
                "fail",
                detail=f"could not import scripts/: {exc}",
                fix="run inside the sealedstack engine image (scripts/ and schemas/ must be present)",
                docs=DOCS_CONFIG,
            )
        ]

    # Point the reused scripts at this context's config/build dirs.
    validate.CONFIG = ctx.config.config_dir
    render_mod.CONFIG = ctx.config.config_dir
    render_mod.BUILD = ctx.build_dir

    results: list[Result] = []

    schema_problems: list[str] = []
    for label, data, schema in (
        ("stack.yaml", ctx.config.stack, "stack.schema.json"),
        ("servers.yaml", ctx.config.servers, "servers.schema.json"),
        ("apps.yaml", ctx.config.apps, "apps.schema.json"),
    ):
        for problem in validate.schema_errors(data, validate.SCHEMAS / schema):
            schema_problems.append(f"config/{label}: {problem}")
    results.append(
        _result(
            "config.schema",
            "config",
            schema_problems,
            fix="fix the listed errors in config/ (see docs/getting-started.md)",
            docs=DOCS_CONFIG,
        )
    )

    cross_problems = validate.check_cross_file(ctx.config.stack, ctx.config.servers, ctx.config.apps)
    results.append(
        _result(
            "config.cross_file",
            "config",
            cross_problems,
            fix="make the config internally consistent (servers, policies, hostnames, mesh)",
            docs=DOCS_CONFIG,
        )
    )

    registry_problems = list(registry_mod.schema_errors()) + list(
        registry_mod.check_selection(ctx.config.stack, ctx.config.servers)
    )
    results.append(
        _result(
            "config.registry",
            "config",
            registry_problems,
            fix="select a supported implementation in stack.yaml/servers.yaml (see `sealedstack debug providers`)",
            docs=DOCS_CONFIG,
        )
    )

    render_problems: list[str] = []
    compose_warnings: list[str] = []
    try:
        rendered = render_mod.render_all(allow_unresolved=True)
    except Exception as exc:  # noqa: BLE001
        render_problems.append(f"render failed: {exc}")
    else:
        for name, app in rendered.items():
            if app.get("_disabled"):
                continue
            if app.get("_unresolved"):
                compose_warnings.append(
                    f"app '{name}': manifest unresolved ({app.get('_reason', 'offline')})"
                )
                continue
            for problem in validate.schema_errors(app["manifest"], validate.SCHEMAS / "sealedstack.schema.json"):
                render_problems.append(f"app '{name}': {problem}")
            if app.get("_compose_error"):
                compose_warnings.append(f"app '{name}': could not fetch compose ({app['_compose_error']})")
            if app.get("_compose"):
                compose_warnings.extend(f"app '{name}': {p}" for p in lint.lint(app["_compose"], app.get("needs")))
            render_problems.extend(f"app '{name}': {p}" for p in (app.get("_build_problems") or []))

    results.append(
        _result(
            "config.render",
            "config",
            render_problems,
            fix="fix the app manifests/registry so they render",
            docs=DOCS_CONFIG,
        )
    )
    results.append(
        _result(
            "config.compose",
            "config",
            compose_warnings,
            warn=True,
            fix="check the app compose (no ports:, image pins on build.mode ci)",
            docs="docs/providers.md",
        )
    )
    return results


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def gather_needed_secrets(stack: dict, servers: dict, registry: dict) -> list[tuple[str, str]]:
    """(secret name, why) for every selected, implemented provider (spec 7)."""
    needed: list[tuple[str, str]] = []
    implementations = registry

    def add(slot: str, impl: str) -> None:
        entry = (implementations.get(slot) or {}).get("implementations", {}).get(impl)
        if not entry or entry.get("status") == "planned":
            return
        for secret in entry.get("secrets") or []:
            needed.append((secret, f"{slot}:{impl}"))

    for slot, impl in (stack.get("slots") or {}).items():
        if impl:
            add(slot, impl)
    for _name, spec in (servers.get("servers") or {}).items():
        if spec.get("provider"):
            add("compute", spec["provider"])
    return needed


def check_secrets(ctx: CheckContext) -> list[Result]:
    results: list[Result] = []
    seen: set[str] = set()
    for name, why in gather_needed_secrets(ctx.config.stack, ctx.config.servers, ctx.registry):
        if name in seen:
            continue
        seen.add(name)
        # D-050: config maps the logical name to its key in the source.
        source_key = ctx.config.secret_key(name)
        label = name if source_key == name else f"{name} (source key {source_key})"
        result = ctx.secrets.get(source_key)
        result_id = f"secrets.{name}"
        if result.errored:
            results.append(
                Result(
                    result_id,
                    "secrets",
                    "fail",
                    detail=f"{why} needs {label}: {result.detail}",
                    fix=f"fix access to the {ctx.secrets.kind} secret source, or export {source_key}",
                    docs=DOCS_SECRETS,
                )
            )
        elif result.missing:
            results.append(
                Result(
                    result_id,
                    "secrets",
                    "fail",
                    detail=f"{why} needs {label}: {result.detail}",
                    fix=f"add {source_key} to the {ctx.secrets.kind} secret source",
                    docs=DOCS_SECRETS,
                )
            )
        else:
            results.append(Result(result_id, "secrets", "ok", detail=f"{why}: {label} present"))

    pw_source_key = ctx.config.secret_key("state_passphrase")
    pw_label = "state_passphrase" if pw_source_key == "state_passphrase" else f"state_passphrase (source key {pw_source_key})"
    passphrase = ctx.secrets.get(pw_source_key)
    if passphrase.errored:
        results.append(
            Result(
                "secrets.state_passphrase",
                "secrets",
                "fail",
                detail=passphrase.detail,
                fix=f"fix access to the secret source, or export {pw_source_key}",
                docs=DOCS_SECRETS,
            )
        )
    elif passphrase.missing:
        results.append(
            Result(
                "secrets.state_passphrase",
                "secrets",
                "fail",
                detail=f"{pw_label} is required to encrypt OpenTofu state (>= 16 chars)",
                fix=f"add {pw_source_key} (>= 16 chars) to the secret source",
                docs=DOCS_SECRETS,
            )
        )
    elif len(passphrase.value) < 16:
        results.append(
            Result(
                "secrets.state_passphrase",
                "secrets",
                "fail",
                detail=f"{pw_label} is {len(passphrase.value)} chars; at least 16 are required",
                fix="regenerate the state passphrase with at least 16 characters",
                docs=DOCS_SECRETS,
            )
        )
    else:
        results.append(Result("secrets.state_passphrase", "secrets", "ok", detail=f"{pw_label} is >= 16 chars"))
    return results


# --------------------------------------------------------------------------- #
# versions
# --------------------------------------------------------------------------- #
def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value or "")
    if not numbers:
        return (0,)
    return tuple(int(part) for part in numbers[:3])


def check_versions(ctx: CheckContext) -> list[Result]:
    version = ctx.version
    newest = NEWEST_KNOWN_VERSION or "unknown (offline)"
    if not version:
        return [
            Result(
                "versions.engine",
                "versions",
                "fail",
                detail="VERSION file is missing from the engine code root",
                fix="reinstall/rebuild the engine image",
                docs="docs/limitations.md",
            )
        ]
    if version in AFFECTED_VERSIONS:
        return [
            Result(
                "versions.engine",
                "versions",
                "fail",
                detail=f"engine {version} is affected by {AFFECTED_VERSIONS[version]}",
                fix="merge the version-bump PR to a fixed release",
                docs="docs/limitations.md",
            )
        ]
    base = f"engine {version}; newest available: {newest}"
    if _version_tuple(version) < _version_tuple(DEPRECATED_BELOW):
        return [
            Result(
                "versions.engine",
                "versions",
                "warn",
                detail=f"{base}; {DEPRECATED_BELOW}+ recommended",
                fix="merge the version-bump PR",
                docs="docs/limitations.md",
            )
        ]
    return [Result("versions.engine", "versions", "ok", detail=base, docs="docs/limitations.md")]


# --------------------------------------------------------------------------- #
# cloudflare
# --------------------------------------------------------------------------- #
def _json_or_none(response: Any) -> dict:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def check_cloudflare(ctx: CheckContext) -> list[Result]:
    token = ctx.env.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        return [
            Result(
                "cloudflare.token",
                "cloudflare",
                "skip",
                detail="CLOUDFLARE_API_TOKEN is not set",
                fix="add CLOUDFLARE_API_TOKEN to the secret source",
                docs=DOCS_CF,
            )
        ]

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    results: list[Result] = []

    token_id = ""
    try:
        response = ctx.http.request("GET", f"{CF_API}/user/tokens/verify", headers=headers)
        data = _json_or_none(response)
    except Exception as exc:  # noqa: BLE001
        return [
            Result(
                "cloudflare.token",
                "cloudflare",
                "fail",
                detail=f"could not reach Cloudflare: {exc}",
                fix="check network access from the runner",
                docs=DOCS_CF,
            )
        ]

    if response.status == 200 and data.get("success"):
        token_id = str((data.get("result") or {}).get("id") or "")
        results.append(Result("cloudflare.token", "cloudflare", "ok", detail="token verified"))
    else:
        results.append(
            Result(
                "cloudflare.token",
                "cloudflare",
                "fail",
                detail=f"token verify failed (HTTP {response.status})",
                fix="recreate CLOUDFLARE_API_TOKEN with the documented permissions",
                docs=DOCS_CF,
            )
        )
        return results

    cloudflare = ctx.config.stack.get("cloudflare") or {}
    account_id = str(cloudflare.get("account_id") or "")
    zone = str(cloudflare.get("zone") or "")

    zone_id = ""
    if zone:
        try:
            response = ctx.http.request("GET", f"{CF_API}/zones?name={zone}", headers=headers)
            data = _json_or_none(response)
            entries = data.get("result") or []
            if response.status == 200 and entries:
                zone_id = str(entries[0].get("id") or "")
        except Exception:  # noqa: BLE001
            pass
    if zone:
        results.append(
            Result(
                "cloudflare.zone",
                "cloudflare",
                "ok" if zone_id else "fail",
                detail=f"zone '{zone}' found" if zone_id else f"zone '{zone}' not found for this token",
                fix="" if zone_id else "check cloudflare.zone and the token's Zone: Read permission",
                docs=DOCS_CF,
            )
        )

    for permission_id, label, path in CF_PERMISSIONS:
        if "{zone_id}" in path and not zone_id:
            results.append(
                Result(
                    f"cloudflare.perm.{permission_id}",
                    "cloudflare",
                    "skip",
                    detail=f"zone id unknown; cannot check {label}",
                    docs=DOCS_CF,
                )
            )
            continue
        if "{account_id}" in path and not account_id:
            results.append(
                Result(
                    f"cloudflare.perm.{permission_id}",
                    "cloudflare",
                    "skip",
                    detail=f"cloudflare.account_id unset; cannot check {label}",
                    docs=DOCS_CF,
                )
            )
            continue
        url = CF_API + path.format(account_id=account_id, zone_id=zone_id)
        try:
            response = ctx.http.request("GET", url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            results.append(
                Result(
                    f"cloudflare.perm.{permission_id}",
                    "cloudflare",
                    "fail",
                    detail=f"{label}: request failed ({exc})",
                    fix="check network access from the runner",
                    docs=DOCS_CF,
                )
            )
            continue
        if response.status == 200:
            results.append(Result(f"cloudflare.perm.{permission_id}", "cloudflare", "ok", detail=f"{label}: ok"))
        elif response.status in (401, 403):
            results.append(
                Result(
                    f"cloudflare.perm.{permission_id}",
                    "cloudflare",
                    "fail",
                    detail=f"{label}: denied (HTTP {response.status})",
                    fix=f"add '{label}' to CLOUDFLARE_API_TOKEN",
                    docs=DOCS_CF,
                )
            )
        else:
            results.append(
                Result(
                    f"cloudflare.perm.{permission_id}",
                    "cloudflare",
                    "warn",
                    detail=f"{label}: unexpected HTTP {response.status}",
                    docs=DOCS_CF,
                )
            )

    if account_id:
        try:
            response = ctx.http.request(
                "GET", f"{CF_API}/accounts/{account_id}/access/organizations", headers=headers
            )
            data = _json_or_none(response)
            ok = response.status == 200 and data.get("success")
            results.append(
                Result(
                    "cloudflare.zero_trust",
                    "cloudflare",
                    "ok" if ok else "fail",
                    detail="Zero Trust is enabled" if ok else f"Zero Trust not reachable (HTTP {response.status})",
                    fix="" if ok else "enable Zero Trust for the account and grant Access permissions",
                    docs=DOCS_CF,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                Result("cloudflare.zero_trust", "cloudflare", "fail", detail=str(exc), docs=DOCS_CF)
            )

    if token_id:
        results.append(
            Result(
                "cloudflare.r2_derived",
                "cloudflare",
                "ok",
                detail="derived R2 keys from the Cloudflare token (key id = token id)",
                docs=DOCS_CF,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# tailscale
# --------------------------------------------------------------------------- #
def _load_hujson(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    try:
        import json5  # type: ignore

        return json5.loads(text)
    except ImportError:
        return json.loads(text)


def _broad_rules(policy: dict, code_root: Optional[Path] = None) -> list[Any]:
    """Allow-all detection (spec 10 F1). Reuses scripts/mesh-policy.py if possible."""
    try:
        module = _load_mesh_policy_module(code_root)
        return module.broad_rules(policy)
    except Exception:  # noqa: BLE001 - fall back to a minimal detector
        broad_dests = {"*", "*:*", "autogroup:*"}
        found = []
        for key in ("grants", "acls"):
            for rule in policy.get(key) or []:
                if any(destination in broad_dests for destination in (rule or {}).get("dst") or []):
                    found.append((key, rule))
        return found


def _load_mesh_policy_module(code_root: Optional[Path] = None) -> Any:
    code_root = Path(code_root) if code_root else cfg.find_code_root()
    path = code_root / "scripts" / "mesh-policy.py"
    spec = importlib.util.spec_from_file_location("_sealedstack_mesh_policy", path)
    if spec is None or spec.loader is None:
        raise ImportError("mesh-policy.py not found")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check_tailscale(ctx: CheckContext) -> list[Result]:
    slots = ctx.config.stack.get("slots") or {}
    if slots.get("mesh") != "tailscale":
        return [
            Result(
                "tailscale.mesh",
                "tailscale",
                "skip",
                detail=f"mesh slot is '{slots.get('mesh', 'unset')}', not tailscale",
                docs=DOCS_TS,
            )
        ]

    results: list[Result] = []
    mesh = ctx.config.stack.get("mesh") or {}
    tailnet = str(mesh.get("tailnet") or "")
    if tailnet:
        results.append(Result("tailscale.tailnet", "tailscale", "ok", detail=f"tailnet {tailnet}"))
    else:
        results.append(
            Result(
                "tailscale.tailnet",
                "tailscale",
                "fail",
                detail="mesh.tailnet is not set",
                fix="set mesh.tailnet to your MagicDNS suffix (e.g. tail1234.ts.net)",
                docs=DOCS_TS,
            )
        )

    policy_kind = mesh.get("policy", "managed")
    policy_file = ctx.config.config_dir / "tailscale-policy.hujson"
    if policy_kind == "unmanaged":
        results.append(
            Result("tailscale.policy", "tailscale", "ok", detail="mesh.policy: unmanaged (block printed by hand)")
        )
    elif policy_file.exists():
        results.append(Result("tailscale.policy", "tailscale", "ok", detail="policy file present"))
    else:
        results.append(
            Result(
                "tailscale.policy",
                "tailscale",
                "fail",
                detail=f"{policy_file} is missing",
                fix="run `sealedstack setup` to import your policy, or set mesh.policy: unmanaged",
                docs=DOCS_TS,
            )
        )

    if policy_file.exists():
        try:
            policy = _load_hujson(policy_file)
            broad = _broad_rules(policy, ctx.config.code_root)
        except Exception as exc:  # noqa: BLE001
            results.append(Result("tailscale.allow_all", "tailscale", "warn", detail=f"could not parse policy: {exc}", docs=DOCS_TS))
        else:
            if broad:
                if mesh.get("narrow_allow_all"):
                    results.append(
                        Result(
                            "tailscale.allow_all",
                            "tailscale",
                            "ok",
                            detail="allow-all rule present; narrow_allow_all rewrites it for members only",
                        )
                    )
                else:
                    results.append(
                        Result(
                            "tailscale.allow_all",
                            "tailscale",
                            "fail",
                            detail="the policy still has an allow-all rule (src/dst *), so servers are not sealed",
                            fix="set mesh.narrow_allow_all: true to rewrite it to autogroup:member → autogroup:member:*",
                            docs=DOCS_TS,
                        )
                    )
            else:
                results.append(Result("tailscale.allow_all", "tailscale", "ok", detail="no allow-all rule"))

    client_id = ctx.env.get("TAILSCALE_OAUTH_CLIENT_ID", "")
    client_secret = ctx.env.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
    if not (client_id and client_secret):
        results.append(
            Result(
                "tailscale.oauth",
                "tailscale",
                "skip",
                detail="TAILSCALE_OAUTH_CLIENT_ID/_SECRET not set",
                fix="create a Tailscale OAuth client with scopes policy_file, auth_keys, devices:core",
                docs=DOCS_TS,
            )
        )
        return results

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    try:
        response = ctx.http.request(
            "POST",
            f"{TS_API}/oauth/token",
            headers={"Authorization": f"Basic {basic}"},
            form_body={"grant_type": "client_credentials"},
        )
        data = _json_or_none(response)
        access_token = str(data.get("access_token") or "")
    except Exception as exc:  # noqa: BLE001
        results.append(Result("tailscale.oauth", "tailscale", "fail", detail=str(exc), docs=DOCS_TS))
        return results

    if response.status != 200 or not access_token:
        results.append(
            Result(
                "tailscale.oauth",
                "tailscale",
                "fail",
                detail=f"OAuth token exchange failed (HTTP {response.status})",
                fix="check the OAuth client id/secret and scopes",
                docs=DOCS_TS,
            )
        )
        return results

    results.append(Result("tailscale.oauth", "tailscale", "ok", detail="OAuth client minted a token"))
    if tailnet:
        try:
            whoami = ctx.http.request(
                "GET",
                f"{TS_API}/tailnet/{tailnet}/devices?limit=1",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            ok = whoami.status == 200
            results.append(
                Result(
                    "tailscale.whoami",
                    "tailscale",
                    "ok" if ok else "fail",
                    detail="tailnet reachable as the OAuth client" if ok else f"devices list failed (HTTP {whoami.status})",
                    fix="" if ok else "ensure the OAuth client can read devices:core for this tailnet",
                    docs=DOCS_TS,
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(Result("tailscale.whoami", "tailscale", "fail", detail=str(exc), docs=DOCS_TS))
    return results


# --------------------------------------------------------------------------- #
# forge
# --------------------------------------------------------------------------- #
def _app_repos(apps: dict) -> list[tuple[str, str]]:
    repos: list[tuple[str, str]] = []
    for name, app in (apps.get("apps") or {}).items():
        repo = (app.get("source") or {}).get("repo")
        if repo:
            repos.append((name, repo))
    return repos


def check_forge(ctx: CheckContext) -> list[Result]:
    forge = str(ctx.config.stack.get("forge") or "github")
    if forge == "gitlab":
        return _check_gitlab(ctx)
    return _check_github(ctx)


def _check_github(ctx: CheckContext) -> list[Result]:
    token = ctx.env.get("GH_TOKEN") or ctx.env.get("GITHUB_TOKEN") or ""
    if not token:
        return [
            Result(
                "forge.token",
                "forge",
                "skip",
                detail="GH_TOKEN/GITHUB_TOKEN is not set",
                fix="add the bot identity token to the secret source",
                docs=DOCS_FORGE,
            )
        ]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results: list[Result] = []
    try:
        response = ctx.http.request("GET", f"{GITHUB_API}/user", headers=headers)
        data = _json_or_none(response)
        ok = response.status == 200
        results.append(
            Result(
                "forge.identity",
                "forge",
                "ok" if ok else "fail",
                detail=f"authenticated as {data.get('login')}" if ok else f"identity check failed (HTTP {response.status})",
                fix="" if ok else "check the GitHub token",
                docs=DOCS_FORGE,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return [Result("forge.identity", "forge", "fail", detail=str(exc), docs=DOCS_FORGE)]

    for _name, repo in _app_repos(ctx.config.apps):
        pr_ok = False
        var_ok = False
        try:
            pr_response = ctx.http.request("GET", f"{GITHUB_API}/repos/{repo}/pulls?state=open&per_page=1", headers=headers)
            pr_ok = pr_response.status == 200
            var_response = ctx.http.request("GET", f"{GITHUB_API}/repos/{repo}/actions/variables?per_page=1", headers=headers)
            var_ok = var_response.status == 200
        except Exception as exc:  # noqa: BLE001
            results.append(
                Result(f"forge.repo.{repo}", "forge", "fail", detail=str(exc), fix="check network access", docs=DOCS_FORGE)
            )
            continue
        if pr_ok and var_ok:
            results.append(Result(f"forge.repo.{repo}", "forge", "ok", detail="can read PRs and Actions variables"))
        else:
            missing = []
            if not pr_ok:
                missing.append("pull_requests")
            if not var_ok:
                missing.append("variables")
            results.append(
                Result(
                    f"forge.repo.{repo}",
                    "forge",
                    "fail",
                    detail=f"bot lacks: {', '.join(missing)}",
                    fix="install the GitHub App on this repo with pull requests and variables (write) permissions",
                    docs=DOCS_FORGE,
                )
            )
    return results


def _check_gitlab(ctx: CheckContext) -> list[Result]:
    token = ctx.env.get("GITLAB_TOKEN") or ctx.env.get("CI_JOB_TOKEN") or ""
    if not token:
        return [
            Result(
                "forge.token",
                "forge",
                "skip",
                detail="GITLAB_TOKEN/CI_JOB_TOKEN is not set",
                fix="add a project/group access token",
                docs=DOCS_FORGE,
            )
        ]
    headers = {"PRIVATE-TOKEN": token}
    results: list[Result] = []
    try:
        response = ctx.http.request("GET", f"{GITLAB_API}/user", headers=headers)
        ok = response.status == 200
        results.append(
            Result(
                "forge.identity",
                "forge",
                "ok" if ok else "fail",
                detail="authenticated" if ok else f"identity check failed (HTTP {response.status})",
                docs=DOCS_FORGE,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return [Result("forge.identity", "forge", "fail", detail=str(exc), docs=DOCS_FORGE)]

    for _name, repo in _app_repos(ctx.config.apps):
        project = repo.replace("/", "%2F")
        try:
            project_response = ctx.http.request("GET", f"{GITLAB_API}/projects/{project}", headers=headers)
            var_response = ctx.http.request("GET", f"{GITLAB_API}/projects/{project}/variables", headers=headers)
            ok = project_response.status == 200 and var_response.status == 200
        except Exception as exc:  # noqa: BLE001
            results.append(Result(f"forge.repo.{repo}", "forge", "fail", detail=str(exc), docs=DOCS_FORGE))
            continue
        results.append(
            Result(
                f"forge.repo.{repo}",
                "forge",
                "ok" if ok else "fail",
                detail="can read the project and its variables" if ok else "cannot read project variables",
                fix="" if ok else "grant the access token api scope on this project",
                docs=DOCS_FORGE,
            )
        )
    return results


# --------------------------------------------------------------------------- #
# servers
# --------------------------------------------------------------------------- #
def check_servers(ctx: CheckContext) -> list[Result]:
    servers = ctx.config.servers.get("servers") or {}
    if not servers:
        return [Result("servers.none", "servers", "skip", detail="no servers configured", docs="docs/limitations.md")]

    tailnet = str((ctx.config.stack.get("mesh") or {}).get("tailnet") or "")
    if not tailnet:
        return [
            Result(
                "servers.tailnet",
                "servers",
                "skip",
                detail="mesh.tailnet is not set; cannot form server FQDNs",
                docs=DOCS_TS,
            )
        ]

    results: list[Result] = []
    for name in servers:
        spec = servers.get(name) or {}
        host = f"{name}.{tailnet}"
        try:
            reachable = bool(ctx.tcp_probe(host, 22, 5.0))
        except Exception as exc:  # noqa: BLE001
            results.append(Result(f"servers.{name}", "servers", "fail", detail=str(exc), docs="docs/limitations.md"))
            continue
        if reachable:
            results.append(
                Result(f"servers.{name}", "servers", "ok", detail=f"{host}:22 reachable over the tailnet")
            )
            continue

        # Not reachable over the tailnet: not bootstrapped yet. For servers
        # sealedstack creates, the IP is learned from the provider; for existing
        # machines the public IP and root password are BOOTSTRAP SECRETS (D-049)
        # and this is the only time they're needed.
        provider = spec.get("provider")
        entry = ((ctx.registry.get("compute") or {}).get("implementations") or {}).get(provider) or {}
        if entry.get("creates_server"):
            results.append(
                Result(
                    f"servers.{name}",
                    "servers",
                    "skip",
                    detail=f"{host}:22 not reachable yet; sealedstack creates this server — run `sealedstack up`",
                    fix="run `sealedstack up`",
                    docs="docs/limitations.md",
                )
            )
            continue

        ip_key, pw_key = ctx.config.bootstrap_secrets(name)
        ip_res = ctx.secrets.get(ip_key)
        pw_res = ctx.secrets.get(pw_key)
        missing = [key for key, res in ((ip_key, ip_res), (pw_key, pw_res)) if not res.found]
        if missing:
            results.append(
                Result(
                    f"servers.{name}",
                    "servers",
                    "fail",
                    detail=f"{host}:22 not reachable and required bootstrap secret(s) missing: {', '.join(missing)}",
                    fix=f"add {', '.join(missing)} to the secret source (the public IP is a secret, D-049)",
                    docs="docs/limitations.md",
                )
            )
        else:
            # Never print the IP; only say the secrets are present.
            results.append(
                Result(
                    f"servers.{name}",
                    "servers",
                    "ok",
                    detail=f"bootstrap secrets present ({ip_key}, {pw_key}); run `sealedstack up` to bootstrap",
                )
            )
    return results


AREA_CHECKS: tuple[Callable[[CheckContext], list[Result]], ...] = (
    check_config,
    check_secrets,
    check_versions,
    check_cloudflare,
    check_tailscale,
    check_forge,
    check_servers,
)


def run_checks(ctx: CheckContext) -> list[Result]:
    results: list[Result] = []
    for check in AREA_CHECKS:
        try:
            results.extend(check(ctx))
        except Exception as exc:  # noqa: BLE001 - a check must never crash the run
            results.append(Result(f"{check.__name__}.error", check.__name__, "fail", detail=str(exc)))
    return results


def check_payload(results: list[Result], *, version: str = "") -> dict:
    counts = Counter(result.status for result in results)
    return {
        "tool": "sealedstack",
        "command": "check",
        "engine_version": version,
        "ok": counts.get("fail", 0) == 0,
        "counts": {status: counts.get(status, 0) for status in ("ok", "warn", "fail", "skip")},
        "results": [result.as_dict() for result in results],
    }


def render_json(results: list[Result], *, version: str = "") -> str:
    return json.dumps(check_payload(results, version=version), indent=2, sort_keys=False)


def render_markdown(results: list[Result], *, title: str = "sealedstack check") -> str:
    counts = Counter(result.status for result in results)
    ready = counts.get("ok", 0) + counts.get("skip", 0)
    total = len(results)
    header = f"Progress: {ready} of {total} ready."
    if counts.get("fail"):
        header += f" {counts['fail']} to fix."
    if counts.get("warn"):
        header += f" {counts['warn']} warning(s)."

    lines = [f"## {title}", "", header, "", "| | Area | Check | Detail |", "|---|---|---|---|"]
    for result in results:
        detail = result.detail or ""
        if result.fix:
            detail += f" — Fix: {result.fix}"
        lines.append(f"| {STATUS_ICON.get(result.status, '?')} | {result.area} | `{result.id}` | {detail} |")
    return "\n".join(lines)
