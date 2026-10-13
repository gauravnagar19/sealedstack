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
from . import r2 as r2_mod
from .http import HttpClient, UrllibClient
from .secrets import SecretSource, build_source

STATUS_ICON = {"ok": "✅", "fail": "❌", "warn": "⚠️", "skip": "⏳"}
STATUS_ORDER = ("fail", "warn", "ok", "skip")

DOCS_CF = "docs/prerequisites.md#cloudflare"
DOCS_TS = "docs/prerequisites.md#tailscale"
DOCS_FORGE = "docs/infra-repo.md"
DOCS_RELEASE_IDENTITY = "docs/ci-cd.md"
DOCS_CONFIG = "docs/getting-started.md"
DOCS_SECRETS = "docs/prerequisites.md"
DOCS_BREAK_GLASS = "docs/user-experience.md"

# D-057: app release jobs join the tailnet as tag:release with their own OIDC trust
# credential. Its client id and audience live in stack.yaml (providers.mesh_tailscale)
# and are published to every app repo under these two variable names.
RELEASE_VARIABLES = ("SEALEDSTACK_TS_RELEASE_CLIENT_ID", "SEALEDSTACK_TS_RELEASE_AUDIENCE")
RELEASE_CREDENTIAL_HOWTO = (
    "the owner creates the `tag:release` OIDC trust credential (runbook T2; Tailscale console, Settings, Trust "
    "credentials, OpenID Connect, issuer GitHub, scope auth_keys write, tag tag:release; see docs/ci-cd.md) and sets "
    "providers.mesh_tailscale.release_oauth_client_id and release_audience in config/stack.yaml"
)

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

    # The MagicDNS suffix and the imported policy file are prerequisites of their own
    # (the `tailscale` area explains and fixes them), not config errors.
    cross_problems = [
        problem
        for problem in validate.check_cross_file(ctx.config.stack, ctx.config.servers, ctx.config.apps)
        if not (problem.startswith("mesh: slots.mesh=tailscale requires mesh.tailnet")
                or (problem.startswith("mesh: ") and "tailscale-policy.hujson is missing" in problem))
    ]
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
def gather_needed_secrets(
    stack: dict,
    servers: dict,
    registry: dict,
    config_dir: Optional[Path] = None,
) -> list[tuple[str, str]]:
    """(secret name, why) for every selected, implemented provider (spec 7).

    A registry entry with ``adapter_config`` needs its ``secrets`` only when that
    config file exists (e.g. the Netcup firewall adapter): the manual-firewall
    path needs no API credentials.
    """
    needed: list[tuple[str, str]] = []
    implementations = registry

    def add(slot: str, impl: str) -> None:
        entry = (implementations.get(slot) or {}).get("implementations", {}).get(impl)
        if not entry or entry.get("status") == "planned":
            return
        adapter = entry.get("adapter_config")
        if adapter and not (config_dir is not None and (Path(config_dir) / adapter).is_file()):
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


def _secret_result(ctx: "CheckContext", logical: str, why: str, *, min_length: int = 0) -> Result:
    """One secret's check result, naming the logical and the source key (D-050)."""
    from . import secrets as secrets_mod

    logical = str(logical).lower()
    label = secrets_mod.describe(ctx.config, logical)
    key = ctx.config.secret_key(logical)
    result_id = f"secrets.{logical}"
    result = secrets_mod.lookup(ctx.secrets, ctx.config, logical)
    project = ctx.config.source_project()
    if result.errored:
        return Result(
            result_id, "secrets", "fail",
            detail=f"{why}: {label}: {result.detail}",
            fix=f"fix access to the {ctx.secrets.kind} secret source, or export {key}",
            docs=DOCS_SECRETS,
        )
    if not result.found:
        extra = f" (at least {min_length} characters)" if min_length else ""
        return Result(
            result_id, "secrets", "fail",
            detail=f"{why}: {label} not found in the {ctx.secrets.kind} source (project '{project}')",
            fix=f"add {key}{extra} to the {ctx.secrets.kind} secret source, project '{project}'",
            docs=DOCS_SECRETS,
        )
    if min_length and len(result.value) < min_length:
        return Result(
            result_id, "secrets", "fail",
            detail=f"{why}: {label} is {len(result.value)} chars; at least {min_length} are required",
            fix=f"regenerate {key} with at least {min_length} characters",
            docs=DOCS_SECRETS,
        )
    if result.name != key:  # served by a deprecated fallback key
        return Result(
            result_id, "secrets", "warn",
            detail=f"{why}: {label} found under the deprecated key '{result.name}'",
            fix=f"rename '{result.name}' to {key} in the secret source (or set secrets.keys.{logical}: {result.name})",
            docs=DOCS_SECRETS,
        )
    return Result(result_id, "secrets", "ok", detail=f"{why}: {label} present")


def _check_secret_source(ctx: CheckContext) -> Result:
    """Can the engine read the secret source at all (a missing token, a bad token)?"""
    kind = ctx.secrets.kind
    label = f"secret source: {kind}"
    if kind == "bitwarden" and not ctx.env.get("BWS_ACCESS_TOKEN"):
        return Result(
            "secrets.source", "secrets", "fail",
            detail="BWS_ACCESS_TOKEN is not set for this repository",
            fix="create a read-only Bitwarden machine account for the project and add its token as the repository "
                "secret BWS_ACCESS_TOKEN (the only secret kept in GitHub)",
            docs=DOCS_SECRETS,
        )
    probe = ctx.secrets.get(ctx.config.secret_key("state_passphrase"))
    if probe.errored:
        return Result(
            "secrets.source", "secrets", "fail", detail=f"{label}: {probe.detail}",
            fix=f"fix access to the {kind} secret source (token, project '{ctx.config.source_project()}')",
            docs=DOCS_SECRETS,
        )
    return Result("secrets.source", "secrets", "ok", detail=f"{label} reachable (project '{ctx.config.source_project()}')")


def check_secrets(ctx: CheckContext) -> list[Result]:
    results: list[Result] = [_check_secret_source(ctx)]
    seen: set[str] = set()
    for name, why in gather_needed_secrets(
        ctx.config.stack, ctx.config.servers, ctx.registry, ctx.config.config_dir
    ):
        logical = name.lower()
        if logical in seen:
            continue
        seen.add(logical)
        results.append(_secret_result(ctx, logical, why))
    results.append(_secret_result(ctx, "state_passphrase", "OpenTofu state encryption", min_length=16))
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
    return _check_engine_version(ctx) + _check_version_pin(ctx)


def _check_version_pin(ctx: CheckContext) -> list[Result]:
    """``sealedstack.version`` must equal the workflow ref (D-031); skipped outside an infra repo."""
    from . import versionpin

    root = Path(ctx.config.config_dir).parent
    if not (root / "sealedstack.version").exists() and not (root / versionpin.WORKFLOW).exists():
        return []
    ok, detail = versionpin.check(root)
    return [
        Result(
            "versions.pin", "versions", "ok" if ok else "fail", detail=detail,
            fix="" if ok else "bump sealedstack.version and the workflow @ref together, in one pull request",
            docs=DOCS_CONFIG,
        )
    ]


def _check_engine_version(ctx: CheckContext) -> list[Result]:
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
def secret_value(ctx: "CheckContext", logical: str) -> str:
    """A secret's value through the configured source (D-050 key mapping), falling
    back to the process environment under the logical name."""
    result = ctx.secrets.get(ctx.config.secret_key(logical))
    if result.found:
        return result.value
    return ctx.env.get(logical, "")


def _json_or_none(response: Any) -> dict:
    try:
        data = response.json()
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def check_cloudflare(ctx: CheckContext) -> list[Result]:
    token = secret_value(ctx, "CLOUDFLARE_API_TOKEN")
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

    results.append(_check_r2_credentials(ctx, results, token_id))
    return results


def _check_r2_credentials(ctx: CheckContext, results: list[Result], token_id: str) -> Result:
    """Report whether R2 credentials really exist (D-054): an explicit override, or
    a derivation that actually succeeds. Never prints any key material."""
    rid = "cloudflare.r2_derived"
    r2_perm = next((res for res in results if res.id == "cloudflare.perm.r2_edit"), None)
    fix = (
        f"add \"{r2_mod.R2_PERMISSION}\" to the Cloudflare token "
        f"({ctx.config.secret_key(r2_mod.TOKEN_KEY)}) at {r2_mod.TOKEN_URL}"
    )

    access = ctx.secrets.get(ctx.config.secret_key(r2_mod.OVERRIDE_ACCESS))
    secret = ctx.secrets.get(ctx.config.secret_key(r2_mod.OVERRIDE_SECRET))
    if access.found and secret.found:
        return Result(rid, "cloudflare", "ok", detail="using the explicit R2 key override from the secret source", docs=DOCS_CF)
    if access.found != secret.found:
        return Result(
            rid, "cloudflare", "fail",
            detail="only one of the optional R2 override keys is set",
            fix=f"set both {ctx.config.secret_key(r2_mod.OVERRIDE_ACCESS)} and "
                f"{ctx.config.secret_key(r2_mod.OVERRIDE_SECRET)}, or delete the one that is set",
            docs=DOCS_CF,
        )

    if not token_id:
        return Result(rid, "cloudflare", "skip", detail="R2 keys are derived from a verified Cloudflare token", docs=DOCS_CF)
    if r2_perm is None or r2_perm.status == "skip":
        return Result(
            rid, "cloudflare", "skip",
            detail="cannot confirm the R2 permission (cloudflare.account_id is not set)",
            fix="set cloudflare.account_id in config/stack.yaml", docs=DOCS_CF,
        )
    if r2_perm.status == "fail":
        return Result(
            rid, "cloudflare", "fail",
            detail="R2 keys cannot be derived: the Cloudflare token lacks Workers R2 Storage: Edit",
            fix=fix, docs=DOCS_CF,
        )
    if r2_perm.status != "ok":
        return Result(rid, "cloudflare", "warn", detail="could not confirm the R2 permission; derivation not verified", fix=fix, docs=DOCS_CF)
    try:
        r2_mod.resolve(ctx.secrets, ctx.config, ctx.env, http=ctx.http)
    except r2_mod.R2Error as exc:
        return Result(rid, "cloudflare", "fail", detail=f"R2 key derivation failed: {str(exc).splitlines()[0]}", fix=fix, docs=DOCS_CF)
    return Result(rid, "cloudflare", "ok", detail="derived from the Cloudflare token (token verified, R2 permission present)", docs=DOCS_CF)


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


def release_identity_settings(ctx: CheckContext) -> tuple[str, str]:
    """The ``tag:release`` client id and audience, as ``publish-app-settings`` reads them."""
    mesh = (ctx.config.stack.get("providers") or {}).get("mesh_tailscale") or {}
    client_id = str(mesh.get("release_oauth_client_id") or ctx.env.get("TS_RELEASE_OAUTH_CLIENT_ID") or "")
    audience = str(mesh.get("release_audience") or ctx.env.get("TS_RELEASE_OAUTH_AUDIENCE") or "")
    return client_id, audience


def _check_release_identity(ctx: CheckContext) -> Result:
    """The infra repo knows the ``tag:release`` OIDC trust credential (D-057).

    It is a manual step in the Tailscale console, needed as soon as an app with its
    own repository exists (image-only apps have no release workflow).
    """
    client_id, audience = release_identity_settings(ctx)
    repo_apps = _app_repos(ctx.config.apps)
    if client_id and audience:
        return Result("tailscale.release_identity", "tailscale", "ok", detail="tag:release OIDC trust credential configured (client id and audience set)")
    missing = [name for name, value in (("release_oauth_client_id", client_id), ("release_audience", audience)) if not value]
    if not repo_apps:
        return Result(
            "tailscale.release_identity", "tailscale", "skip",
            detail="no app has its own repository yet, so no release job needs the tag:release credential",
            fix=f"before the first app release: {RELEASE_CREDENTIAL_HOWTO}",
            docs=DOCS_RELEASE_IDENTITY,
        )
    return Result(
        "tailscale.release_identity", "tailscale", "fail",
        detail=f"providers.mesh_tailscale.{' and '.join(missing)} not set, but {len(repo_apps)} app(s) have a release workflow "
               f"({', '.join(name for name, _repo in repo_apps)})",
        fix=RELEASE_CREDENTIAL_HOWTO,
        docs=DOCS_RELEASE_IDENTITY,
    )


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

    results.append(_check_release_identity(ctx))

    client_id = secret_value(ctx, "TAILSCALE_OAUTH_CLIENT_ID")
    client_secret = secret_value(ctx, "TAILSCALE_OAUTH_CLIENT_SECRET")
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


def _forge_repos(ctx: CheckContext) -> list[tuple[str, str]]:
    """(repo, kind) for the infra repo (when known) and every onboarded app repo."""
    repos: list[tuple[str, str]] = []
    infra = str(ctx.env.get("GITHUB_REPOSITORY") or "")
    if infra:
        repos.append((infra, "infra"))
    for _name, repo in _app_repos(ctx.config.apps):
        if all(repo != known for known, _kind in repos):
            repos.append((repo, "app"))
    return repos


# Harmless reads: (permission, path, applies to). A read that returns 403 means the
# token lacks that permission on the repository. Write levels cannot be read from
# the API (a fine-grained token does not describe itself), so they are not probed.
_GITHUB_READ_PROBES = (
    ("Contents", "/repos/{repo}/contents/", ("infra", "app")),
    ("Pull requests", "/repos/{repo}/pulls?state=open&per_page=1", ("infra", "app")),
    ("Issues", "/repos/{repo}/issues?per_page=1", ("infra", "app")),
    ("Variables", "/repos/{repo}/actions/variables?per_page=1", ("app",)),
    ("Deployments", "/repos/{repo}/deployments?environment=production&per_page=1", ("app",)),
)


def _check_github(ctx: CheckContext) -> list[Result]:
    from . import botpermissions

    token = ctx.env.get("GH_TOKEN") or ctx.env.get("GITHUB_TOKEN") or ""
    if not token:
        return [
            Result(
                "forge.token",
                "forge",
                "skip",
                detail="GH_TOKEN/GITHUB_TOKEN is not set",
                fix="add the bot identity: a fine-grained personal access token as the repository secret GH_TOKEN (D-058)",
                docs=DOCS_FORGE,
            )
        ]
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    results: list[Result] = []
    kind = botpermissions.token_kind(token)
    if kind == "classic":
        results.append(
            Result(
                "forge.token_type", "forge", "warn",
                detail="GH_TOKEN is a classic personal access token: it works, but it reaches every repository you can",
                fix="replace it with a fine-grained token limited to the infra repo and the app repos "
                    f"({botpermissions.short_list()})",
                docs=DOCS_FORGE,
            )
        )
    try:
        response = ctx.http.request("GET", f"{GITHUB_API}/user", headers=headers)
        data = _json_or_none(response)
        ok = response.status == 200
        # The built-in Actions token is not a user: /user is refused, so skip the
        # identity line for it (the bot row explains the consequence).
        if ok or kind != "builtin":
            results.append(
                Result(
                    "forge.identity",
                    "forge",
                    "ok" if ok else "fail",
                    detail=f"authenticated as {data.get('login')}" if ok else f"identity check failed (HTTP {response.status})",
                    fix="" if ok else "check the GitHub token (expired, revoked or mistyped)",
                    docs=DOCS_FORGE,
                )
            )
    except Exception as exc:  # noqa: BLE001
        return results + [Result("forge.identity", "forge", "fail", detail=str(exc), docs=DOCS_FORGE)]

    for repo, repo_kind in _forge_repos(ctx):
        results.append(_check_github_repo(ctx, headers, repo, repo_kind))
    if (ctx.config.stack.get("slots") or {}).get("mesh") == "tailscale":
        seen: set[str] = set()
        for name, repo in _app_repos(ctx.config.apps):
            if repo not in seen:
                seen.add(repo)
                results.append(_check_release_variables(ctx, headers, name, repo))
    return results


def _check_release_variables(ctx: CheckContext, headers: Mapping[str, str], app: str, repo: str) -> Result:
    """The app repo has the ``tag:release`` variables its release job needs (D-057)."""
    rid = f"forge.release_vars.{repo}"
    missing: list[str] = []
    unreadable = ""
    for name in RELEASE_VARIABLES:
        try:
            response = ctx.http.request("GET", f"{GITHUB_API}/repos/{repo}/actions/variables/{name}", headers=headers)
        except Exception as exc:  # noqa: BLE001
            return Result(rid, "forge", "fail", detail=f"{repo}: {exc}", fix="check network access", docs=DOCS_FORGE)
        if response.status == 200 and str(_json_or_none(response).get("value") or ""):
            continue
        if response.status in (200, 404):
            missing.append(name)
        else:
            unreadable = f"HTTP {response.status}"
    if missing:
        return Result(
            rid, "forge", "fail",
            detail=f"{repo} ({app}) has no {' / '.join(missing)}: its release job cannot join the tailnet as tag:release",
            fix=f"run apply to publish them from config/stack.yaml; if they are not set there yet, {RELEASE_CREDENTIAL_HOWTO}",
            docs=DOCS_RELEASE_IDENTITY,
        )
    if unreadable:
        return Result(
            rid, "forge", "warn",
            detail=f"could not read the release variables of {repo} ({unreadable})",
            fix="grant the token Variables: read on that repository",
            docs=DOCS_FORGE,
        )
    return Result(rid, "forge", "ok", detail=f"{repo}: release variables published ({', '.join(RELEASE_VARIABLES)})")


def _check_github_repo(ctx: CheckContext, headers: Mapping[str, str], repo: str, repo_kind: str) -> Result:
    from . import botpermissions

    rid = f"forge.repo.{repo}"
    try:
        head = ctx.http.request("GET", f"{GITHUB_API}/repos/{repo}", headers=headers)
        if head.status in (403, 404):
            return Result(
                rid, "forge", "fail",
                detail=f"the token cannot see {repo} (HTTP {head.status})",
                fix=f"add {repo} under Repository access of the fine-grained token (or fix the repo name in apps.yaml)",
                docs=DOCS_FORGE,
            )
        role = _json_or_none(head).get("permissions")
        if isinstance(role, dict) and role.get("push") is False:
            return Result(
                rid, "forge", "fail",
                detail=f"the token's user has no push access to {repo}",
                fix="the account that owns the token needs write access to this repository",
                docs=DOCS_FORGE,
            )
        missing: list[str] = []
        proven: list[str] = []
        for permission, path, applies in _GITHUB_READ_PROBES:
            if repo_kind not in applies:
                continue
            response = ctx.http.request("GET", f"{GITHUB_API}{path.format(repo=repo)}", headers=headers)
            # An empty repository answers /contents/ with 404; that is not a missing permission.
            if response.status == 200 or (permission == "Contents" and response.status == 404):
                proven.append(permission)
            else:
                missing.append(permission)
        warn = ""
        if repo_kind == "app":
            env_response = ctx.http.request(
                "GET", f"{GITHUB_API}/repos/{repo}/environments/production/secrets/public-key", headers=headers
            )
            if env_response.status == 200:
                proven.append("Environments")
            elif env_response.status == 404:
                warn = (
                    "the `production` environment does not exist yet: create it in the repo's Settings, Environments "
                    "(or grant the token Administration: write so sealedstack can)"
                )
            else:
                missing.append("Environments")
    except Exception as exc:  # noqa: BLE001
        return Result(rid, "forge", "fail", detail=str(exc), fix="check network access", docs=DOCS_FORGE)
    if missing:
        needed = {p.name: p for p in botpermissions.required()}
        names = ", ".join(f"{name}: {'read and write' if needed[name].access == 'write' else 'read'}" for name in missing)
        return Result(
            rid, "forge", "fail",
            detail=f"the token lacks permissions on {repo}: {', '.join(missing)}",
            fix=f"edit the fine-grained token and grant {names} (full list: {botpermissions.short_list()})",
            docs=DOCS_FORGE,
        )
    detail = f"{repo}: read access verified ({', '.join(proven)}); write levels are confirmed on first use"
    if warn:
        return Result(rid, "forge", "warn", detail=f"{detail}; {warn}", fix=warn, docs=DOCS_FORGE)
    return Result(rid, "forge", "ok", detail=detail)


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

    results: list[Result] = []
    for name in servers:
        spec = servers.get(name) or {}
        host = f"{name}.{tailnet}" if tailnet else name
        try:
            # Without the MagicDNS suffix nothing can be reached over the tailnet
            # yet: the server counts as not bootstrapped.
            reachable = bool(ctx.tcp_probe(host, 22, 5.0)) if tailnet else False
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
            # Never print the IP. The first bootstrap logs in over the public IP,
            # so TCP 22 must be open there (temporarily, at the provider).
            ip = ip_res.value.strip()
            try:
                open_22 = bool(ip) and bool(ctx.tcp_probe(ip, 22, 5.0))
            except Exception:  # noqa: BLE001
                open_22 = False
            if open_22:
                results.append(
                    Result(
                        f"servers.{name}",
                        "servers",
                        "ok",
                        detail=f"bootstrap secrets present ({ip_key}, {pw_key}) and TCP 22 is open; "
                               "run `sealedstack up` to bootstrap",
                    )
                )
            else:
                results.append(
                    Result(
                        f"servers.{name}",
                        "servers",
                        "fail",
                        detail=f"bootstrap secrets present ({ip_key}, {pw_key}) but TCP 22 is not reachable on the server's public IP",
                        fix="open TCP 22 temporarily at the provider's firewall (close it again after the first `up`); "
                            "check that the IP in the secret is current",
                        docs="docs/netcup-firewall.md",
                    )
                )
    return results


def check_bot(ctx: CheckContext) -> list[Result]:
    """The bot identity (D-058): a fine-grained personal access token in ``GH_TOKEN``.

    The GitHub App path (D-039) is not built yet: its credentials in the secret
    source are not used, so they do not satisfy this row.
    """
    from . import botpermissions
    from . import secrets as secrets_mod

    token = ctx.env.get("GH_TOKEN") or ""
    kind = botpermissions.token_kind(token) if token else ""
    flagged = ctx.env.get("SEALEDSTACK_BOT_TOKEN_SET", "").lower() == "true"
    if (token and kind != "builtin") or flagged:
        label = {"fine-grained": "fine-grained token", "classic": "classic token"}.get(kind, "token")
        return [Result("forge.bot", "forge", "ok", detail=f"bot identity configured ({label} in GH_TOKEN)")]
    app_keys = ("SEALEDSTACK_BOT_APP_ID", "bot_private_key", "SEALEDSTACK_BOT_INSTALLATION_ID")
    have_app = all(secrets_mod.lookup(ctx.secrets, ctx.config, key).found for key in app_keys)
    why = (
        "GH_TOKEN is the workflow's built-in token (its pull requests do not start the plan workflows)"
        if kind == "builtin"
        else "no bot identity: its pull requests are what start the plan workflows (the built-in token's do not)"
    )
    if have_app:
        why += "; the GitHub App credentials in the secret source are not used yet (D-058)"
    return [
        Result(
            "forge.bot", "forge", "fail",
            detail=why,
            fix="create a fine-grained personal access token named sealedstack-bot with these repository permissions on the "
                f"infra repo and the app repos: {botpermissions.short_list()}; store it as the repository secret GH_TOKEN",
            docs=DOCS_FORGE,
        )
    ]


AREA_CHECKS: tuple[Callable[[CheckContext], list[Result]], ...] = (
    check_config,
    check_secrets,
    check_versions,
    check_cloudflare,
    check_tailscale,
    check_forge,
    check_bot,
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
