"""App-side onboarding: turn a plain app repo into a sealedstack app (UX section 6).

``generate`` takes the files of an app repo (as text) and returns the files to
add, change and delete in an onboarding pull request:

* ``sealedstack.yaml`` (the portable manifest, D-017),
* the compose file, transformed (D-024, D-030): no ``ports:`` (traffic arrives
  through the tunnel), no in-compose database when sealedstack manages it
  (D-018), and ``image:`` pinned for every service that has ``build:``,
* ``docker-compose.override.yml`` carrying what was removed (the ports and the
  database), so ``docker compose up --build`` keeps working for developers
  while sealedstack ignores that file,
* ``.github/workflows/release.yml`` calling the reusable release workflow, and
  removal of the workflow it replaces.

The compose file is edited **textually**, not re-serialised, so comments and
formatting survive and the diff in the pull request is small. The result is
checked by parsing it again.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping, Optional

import yaml

COMPOSE_CANDIDATES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
OVERRIDE_PATH = "docker-compose.override.yml"
RELEASE_WORKFLOW = ".github/workflows/release.yml"
REPLACED_WORKFLOWS = (".github/workflows/deploy.yml", ".github/workflows/deploy.yaml")
MANIFEST_PATH = "sealedstack.yaml"

DB_KINDS = {"postgres": re.compile(r"(^|/)(postgres|postgis/postgis|bitnami/postgresql)(:|$)"),
            "redis": re.compile(r"(^|/)(redis|valkey/valkey|bitnami/redis)(:|$)")}
SECRETISH = re.compile(r"(SECRET|TOKEN|PASSWORD|API_?KEY|PRIVATE_?KEY)", re.I)
# Variables sealedstack itself provides; never treated as app secrets.
PROVIDED = {"APP_VERSION"}


class OnboardError(RuntimeError):
    """The app cannot be onboarded automatically; the message says what to do."""


# --------------------------------------------------------------------------- #
# small YAML line helpers
# --------------------------------------------------------------------------- #
def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _skippable(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


_KEY = re.compile(r"""^(?P<indent> *)(?P<key>"[^"]+"|'[^']+'|[A-Za-z0-9_.\-]+)\s*:(?:\s|$)""")


def _key(line: str) -> Optional[str]:
    match = _KEY.match(line)
    if not match:
        return None
    return match.group("key").strip("\"'")


def _span_end(lines: list[str], start: int) -> int:
    """Exclusive end of the block that starts at ``lines[start]`` (a ``key:`` line)."""
    base = _indent(lines[start])
    end = start + 1
    last = start + 1
    while end < len(lines):
        line = lines[end]
        if _skippable(line):
            end += 1
            continue
        indent = _indent(line)
        if indent > base or (indent == base and line.lstrip().startswith("- ")):
            end += 1
            last = end
            continue
        break
    return last


def _children(lines: list[str], start: int, end: int) -> list[tuple[str, int, int]]:
    """The direct ``key:`` children of the block ``lines[start:end]`` (start = parent line)."""
    child_indent = None
    for idx in range(start + 1, end):
        if not _skippable(lines[idx]):
            child_indent = _indent(lines[idx])
            break
    if child_indent is None:
        return []
    out: list[tuple[str, int, int]] = []
    idx = start + 1
    while idx < end:
        line = lines[idx]
        if not _skippable(line) and _indent(line) == child_indent and _key(line) is not None:
            stop = min(_span_end(lines, idx), end)
            out.append((_key(line) or "", idx, stop))
            idx = stop
        else:
            idx += 1
    return out


def _top(lines: list[str], name: str) -> Optional[tuple[int, int]]:
    for idx, line in enumerate(lines):
        if not _skippable(line) and _indent(line) == 0 and _key(line) == name:
            return idx, _span_end(lines, idx)
    return None


def _service_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    top = _top(lines, "services")
    if top is None:
        raise OnboardError("the compose file has no top-level 'services:'")
    return {name: (s, e) for name, s, e in _children(lines, top[0], top[1])}


def _property(lines: list[str], service: tuple[int, int], prop: str) -> Optional[tuple[int, int]]:
    for name, s, e in _children(lines, service[0], service[1]):
        if name == prop:
            return s, e
    return None


def _drop(lines: list[str], start: int, end: int, *, with_comments: bool = False) -> list[str]:
    """Remove ``lines[start:end]`` and return the removed lines."""
    if with_comments:
        # Also take comment lines directly above (they describe the removed block).
        while start > 0 and lines[start - 1].strip().startswith("#") and _indent(lines[start - 1]) == _indent(lines[start]):
            start -= 1
    removed = lines[start:end]
    del lines[start:end]
    return removed


def _dedent(block: list[str]) -> str:
    base = min((_indent(line) for line in block if line.strip()), default=0)
    return "\n".join(line[base:] if line.strip() else "" for line in block) + "\n"


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Removed:
    """What the transformation took out, kept so the override can restore it for dev."""

    ports: dict[str, list[str]] = field(default_factory=dict)  # service -> original port entries
    services: dict[str, list[str]] = field(default_factory=dict)  # service -> original block lines
    depends_on: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # service -> {dep: lines}
    env: dict[str, dict[str, str]] = field(default_factory=dict)  # service -> {KEY: original value}
    volumes: dict[str, list[str]] = field(default_factory=dict)  # volume name -> original lines


@dataclass
class Needs:
    postgres: Optional[dict] = None
    redis: Optional[dict] = None


@dataclass
class ComposeResult:
    text: str
    changes: list[str]
    removed: Removed
    needs: Needs
    web_service: str
    web_port: int
    health: str
    images: dict[str, str]  # service -> pinned image
    secrets: list[str]
    notes: list[str]


@dataclass
class Plan:
    """Everything the onboarding PR changes in the app repo."""

    files: dict[str, str]
    deletes: list[str]
    changes: list[str]
    manifest: dict
    notes: list[str]
    body: str = ""


def _service_kind(spec: dict) -> Optional[str]:
    image = str((spec or {}).get("image") or "")
    for kind, pattern in DB_KINDS.items():
        if pattern.search(image):
            return kind
    return None


def _container_port(entry: object) -> Optional[int]:
    if isinstance(entry, dict):
        target = entry.get("target")
        return int(target) if target else None
    text = str(entry).split("/")[0]
    last = text.split(":")[-1]
    first = last.split("-")[0]
    return int(first) if first.isdigit() else None


def _port_entry_text(entry: object) -> str:
    if isinstance(entry, dict):
        published = entry.get("published")
        target = entry.get("target")
        return f"{published}:{target}" if published else str(target)
    return str(entry)


def _env_map(spec: dict) -> dict[str, str]:
    env = (spec or {}).get("environment") or {}
    if isinstance(env, list):
        out = {}
        for item in env:
            key, _, value = str(item).partition("=")
            out[key] = value
        return out
    return {str(k): "" if v is None else str(v) for k, v in env.items()}


def _health_path(spec: dict) -> str:
    test = ((spec or {}).get("healthcheck") or {}).get("test")
    text = " ".join(test) if isinstance(test, list) else str(test or "")
    match = re.search(r"https?://[^/\s'\"]+(/[^\s'\"]*)", text)
    return match.group(1) if match else ""


def _pick_web(services: dict, ports: dict[str, list[object]], web_service: str) -> str:
    if web_service:
        if web_service not in services:
            raise OnboardError(f"--web-service '{web_service}' is not a service in the compose file ({', '.join(services)})")
        return web_service
    app_services = {n: s for n, s in services.items() if _service_kind(s) is None}
    for name in app_services:
        if ports.get(name):
            return name
    for preferred in ("web", "ui", "frontend", "app", "site"):
        if preferred in app_services:
            return preferred
    for name, spec in app_services.items():
        if isinstance(spec, dict) and "build" in spec:
            return name
    if app_services:
        return next(iter(app_services))
    raise OnboardError("no application service found in the compose file")


# --------------------------------------------------------------------------- #
# compose transformation
# --------------------------------------------------------------------------- #
def transform_compose(
    text: str,
    *,
    name: str,
    repo_slug: str,
    registry_prefix: str,
    web_service: str = "",
    web_port: int = 0,
    health: str = "",
) -> ComposeResult:
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise OnboardError(f"the compose file is not valid YAML: {exc}") from exc
    services = data.get("services")
    if not isinstance(services, dict) or not services:
        raise OnboardError("the compose file has no services")

    lines = text.split("\n")
    changes: list[str] = []
    notes: list[str] = []
    removed = Removed()
    needs = Needs()

    # ---- semantic pass: ports, database services, web service ------------- #
    ports = {n: list((s or {}).get("ports") or []) for n, s in services.items() if isinstance(s, dict)}
    db_services = {n: _service_kind(s) for n, s in services.items() if isinstance(s, dict) and _service_kind(s)}
    web = _pick_web(services, ports, web_service)
    port = web_port
    if not port:
        for entry in ports.get(web, []):
            port = _container_port(entry) or 0
            if port:
                break
    if not port:
        expose = (services.get(web) or {}).get("expose") or []
        port = _container_port(expose[0]) if expose else 0
    if not port:
        raise OnboardError(
            f"cannot tell which port the '{web}' service listens on (it publishes none); "
            "re-run with --web-port"
        )
    health_path = health or _health_path(services.get(web) or {})

    for db_name, kind in db_services.items():
        spec = services[db_name]
        image = str(spec.get("image") or "")
        version_match = re.search(r":(\d+(?:\.\d+)?)", image)
        env_name = ""
        for other, other_spec in services.items():
            if other in db_services or not isinstance(other_spec, dict):
                continue
            for key, value in _env_map(other_spec).items():
                if re.search(rf"@{re.escape(db_name)}\b|//{re.escape(db_name)}\b", value):
                    env_name = key
        if kind == "postgres":
            database = _env_map(spec).get("POSTGRES_DB", "")
            if not database or "$" in database:
                database = re.sub(r"[^a-z0-9_]", "", name.lower().replace("-", "")) or "app"
            needs.postgres = {
                "version": version_match.group(1) if version_match else "16",
                "database": database,
                "env": env_name or "DATABASE_URL",
            }
        else:
            needs.redis = {"env": env_name or "REDIS_URL"}
            if version_match:
                needs.redis["version"] = version_match.group(1)

    # ---- textual pass ------------------------------------------------------ #
    # 1. ports
    for service, entries in ports.items():
        if not entries:
            continue
        span = _service_spans(lines)[service]
        prop = _property(lines, span, "ports")
        if prop:
            _drop(lines, prop[0], prop[1])
            removed.ports[service] = [_port_entry_text(e) for e in entries]
    if removed.ports:
        names = ", ".join(f"`{s}`" for s in removed.ports)
        changes.append(
            f"removed `ports:` from {names} (traffic arrives through the tunnel; published ports would be exposed)"
        )

    # 2. depends_on that reference a removed database
    for service in list(_service_spans(lines)):
        if service in db_services:
            continue
        span = _service_spans(lines)[service]
        dep = _property(lines, span, "depends_on")
        if not dep:
            continue
        depends_on = (services.get(service) or {}).get("depends_on")
        header = lines[dep[0]]
        inline = header.split(":", 1)[1].split("#", 1)[0].strip()
        if inline:
            # flow style on the key line: `depends_on: [db, api]`
            hits = [d for d in depends_on if d in db_services]
            if not hits:
                continue
            keep = [d for d in depends_on if d not in db_services]
            for hit in hits:
                removed.depends_on.setdefault(service, {})[hit] = [f"- {hit}"] if isinstance(depends_on, list) else [f"{hit}:"]
            if keep:
                if isinstance(depends_on, list):
                    value = "[" + ", ".join(keep) + "]"
                else:
                    value = yaml.safe_dump({k: depends_on[k] for k in keep}, default_flow_style=True).strip()
                lines[dep[0]] = f"{header.split(':', 1)[0]}: {value}"
            else:
                _drop(lines, dep[0], dep[1])
            continue
        if isinstance(depends_on, list):
            hits = [d for d in depends_on if d in db_services]
            if not hits:
                continue
            for hit in hits:
                for idx in range(dep[0] + 1, dep[1]):
                    if re.match(rf"^\s*-\s*['\"]?{re.escape(hit)}['\"]?\s*(#.*)?$", lines[idx]):
                        removed.depends_on.setdefault(service, {})[hit] = [lines[idx]]
                        del lines[idx]
                        break
        elif isinstance(depends_on, dict):
            for hit in [d for d in depends_on if d in db_services]:
                span = _service_spans(lines)[service]
                dep = _property(lines, span, "depends_on")
                for child, cs, ce in _children(lines, dep[0], dep[1]):
                    if child == hit:
                        removed.depends_on.setdefault(service, {})[hit] = _drop(lines, cs, ce)
                        break
        # drop the key if nothing is left
        span = _service_spans(lines)[service]
        dep = _property(lines, span, "depends_on")
        if dep and not any(not _skippable(line) for line in lines[dep[0] + 1 : dep[1]]):
            if not lines[dep[0]].split(":", 1)[1].strip():
                _drop(lines, dep[0], dep[1])

    # 3. the database services themselves
    for db_name, kind in db_services.items():
        span = _service_spans(lines)[db_name]
        block = _drop(lines, span[0], span[1], with_comments=True)
        removed.services[db_name] = block
        spec = services[db_name]
        if kind == "postgres":
            changes.append(
                f"removed the `{db_name}` service (sealedstack runs a managed, backed-up Postgres and injects "
                f"`{(needs.postgres or {}).get('env')}`)"
            )
        else:
            changes.append(
                f"removed the `{db_name}` service (sealedstack runs a managed Redis and injects "
                f"`{(needs.redis or {}).get('env')}`)"
            )
        # named volumes used only by this service
        for mount in spec.get("volumes") or []:
            source = str(mount.get("source") if isinstance(mount, dict) else str(mount).split(":")[0])
            others = any(
                source == (str(m.get("source")) if isinstance(m, dict) else str(m).split(":")[0])
                for n, s in services.items()
                if n not in db_services and isinstance(s, dict)
                for m in (s.get("volumes") or [])
            )
            top = _top(lines, "volumes")
            if top and source in (data.get("volumes") or {}) and not others:
                for vol, vs, ve in _children(lines, top[0], top[1]):
                    if vol == source:
                        removed.volumes[vol] = _drop(lines, vs, ve)
                        break
    top = _top(lines, "volumes")
    if top and not _children(lines, top[0], top[1]) and not lines[top[0]].split(":", 1)[1].strip():
        _drop(lines, top[0], top[1])

    # 4. env values that pointed at a removed database now come from sealedstack
    for kind, hint in (("postgres", needs.postgres), ("redis", needs.redis)):
        if not hint:
            continue
        key_name = hint["env"]
        for service, spec in services.items():
            if service in db_services or not isinstance(spec, dict):
                continue
            current = _env_map(spec)
            if key_name not in current or current[key_name] == "${%s}" % key_name:
                continue
            span = _service_spans(lines)[service]
            env_prop = _property(lines, span, "environment")
            if not env_prop:
                continue
            for idx in range(env_prop[0] + 1, env_prop[1]):
                line = lines[idx]
                map_match = re.match(rf"^(\s*){re.escape(key_name)}\s*:\s*(.*)$", line)
                list_match = re.match(rf"^(\s*-\s*){re.escape(key_name)}=(.*)$", line)
                if map_match:
                    removed.env.setdefault(service, {})[key_name] = current[key_name]
                    lines[idx] = f"{map_match.group(1)}{key_name}: ${{{key_name}}}"
                    break
                if list_match:
                    removed.env.setdefault(service, {})[key_name] = current[key_name]
                    lines[idx] = f"{list_match.group(1)}{key_name}=${{{key_name}}}"
                    break
            changes.append(f"`{service}`: `{key_name}` is now provided by sealedstack (`${{{key_name}}}`)")

    # 5. image: for every service that builds
    images: dict[str, str] = {}
    pinned = []
    for service, (start, _end) in _service_spans(lines).items():
        spec = services.get(service)
        if not isinstance(spec, dict) or "build" not in spec:
            continue
        image = f"{registry_prefix}/{repo_slug}-{service}:${{APP_VERSION}}"
        images[service] = image
        span = _service_spans(lines)[service]
        existing = _property(lines, span, "image")
        if existing:
            if str(spec.get("image")) == image:
                continue
            ind = " " * _indent(lines[existing[0]])
            lines[existing[0] : existing[1]] = [f"{ind}image: {image}"]
        else:
            build = _property(lines, span, "build")
            ind = " " * _indent(lines[build[0]])
            lines.insert(build[1], f"{ind}image: {image}")
        pinned.append(service)
    if pinned:
        if len(pinned) == 1:
            shown = images[pinned[0]]
        else:
            tail = ",".join(pinned)
            shown = f"{registry_prefix}/{repo_slug}-{{{tail}}}:${{APP_VERSION}}"
        changes.append(f"added `image: {shown}` (`build:` kept for local development)")

    new_text = "\n".join(lines).rstrip("\n") + ("\n" if text.endswith("\n") else "")
    # a final parse proves the edits left valid YAML with the intended shape
    try:
        parsed = yaml.safe_load(new_text) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - defensive
        raise OnboardError(f"internal error: the transformed compose is not valid YAML ({exc})") from exc
    for service, spec in (parsed.get("services") or {}).items():
        if isinstance(spec, dict) and "ports" in spec:  # pragma: no cover - defensive
            raise OnboardError(f"internal error: 'ports:' left on '{service}'")

    # app secrets: ${VAR} without a default, secret-looking, not managed
    managed = set(PROVIDED) | {(needs.postgres or {}).get("env", ""), (needs.redis or {}).get("env", "")}
    secrets = sorted(
        {
            m
            for m in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", new_text)
            if m not in managed and SECRETISH.search(m)
        }
    )
    other_vars = sorted(
        {
            m
            for m in re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", new_text)
            if m not in managed and m not in secrets
        }
    )
    if other_vars:
        notes.append(
            "variables referenced in the compose file that sealedstack does not provide: "
            + ", ".join(f"`{v}`" for v in other_vars)
            + " (add them to `sealedstack.yaml: secrets` if they are secrets)"
        )
    others_with_ports = [s for s in removed.ports if s != web]
    if others_with_ports:
        notes.append(
            "only `%s` is routed from the internet; %s published ports before and is now reachable "
            "by the other services only" % (web, ", ".join(f"`{s}`" for s in others_with_ports))
        )

    return ComposeResult(
        text=new_text,
        changes=changes,
        removed=removed,
        needs=needs,
        web_service=web,
        web_port=port,
        health=health_path,
        images=images,
        secrets=secrets,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# the local-development override
# --------------------------------------------------------------------------- #
def render_override(result: ComposeResult, original_text: str) -> str:
    """A compose override that restores ports and the database for development."""
    removed = result.removed
    out = [
        "# Local development only. `docker compose up --build` loads this file",
        "# automatically; sealedstack ignores it and deploys docker-compose.yml.",
        "# It restores what sealedstack removes from the deployed compose file:",
        "# published ports and the in-compose database.",
        "services:",
    ]
    touched = list(dict.fromkeys([*removed.ports, *removed.env, *removed.depends_on]))
    for service in touched:
        out.append(f"  {service}:")
        if service in removed.ports:
            out.append("    ports:")
            out.extend(f'      - "{entry}"' for entry in removed.ports[service])
        if service in removed.env:
            out.append("    environment:")
            for key, value in removed.env[service].items():
                out.append(f"      {key}: {_yaml_scalar(value)}")
        if service in removed.depends_on:
            deps = removed.depends_on[service]
            if all(len(block) == 1 and block[0].lstrip().startswith("-") for block in deps.values()):
                out.append("    depends_on:")
                out.extend(f"      - {name}" for name in deps)
            else:
                out.append("    depends_on:")
                for name, block in deps.items():
                    out.extend(_reindent(block, 6))
    for service, block in removed.services.items():
        out.extend(_reindent(block, 2))
    if removed.volumes:
        out.append("volumes:")
        for _vol, block in removed.volumes.items():
            out.extend(_reindent(block, 2))
    return "\n".join(out).rstrip("\n") + "\n"


def _yaml_scalar(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _reindent(block: list[str], indent: int) -> list[str]:
    base = min((_indent(line) for line in block if line.strip()), default=0)
    return [(" " * indent + line[base:]) if line.strip() else "" for line in block]


# --------------------------------------------------------------------------- #
# manifest, workflow and the whole plan
# --------------------------------------------------------------------------- #
def render_manifest(name: str, compose_path: str, result: ComposeResult, *, backup_every: str = "6h", backup_keep: int = 28) -> tuple[str, dict]:
    web = {"service": result.web_service, "port": result.web_port}
    if result.health:
        web["health"] = result.health
    manifest: dict = {"name": name, "compose": compose_path, "web": web, "build": {"mode": "ci", "platforms": ["linux/amd64"]}}
    needs: dict = {}
    if result.needs.postgres:
        needs["postgres"] = {**result.needs.postgres, "backup": {"every": backup_every, "keep": backup_keep}}
    if result.needs.redis:
        needs["redis"] = dict(result.needs.redis)
    if needs:
        manifest["needs"] = needs
    if result.secrets:
        manifest["secrets"] = list(result.secrets)

    lines = [f"name: {name}", f"compose: {compose_path}"]
    health = f", health: {web['health']}" if "health" in web else ""
    lines.append(f"web: {{ service: {web['service']}, port: {web['port']}{health} }}")
    lines += ["build:", "  mode: ci                    # build in CI, deploy the prebuilt image (D-030)", "  platforms: [linux/amd64]"]
    if needs:
        lines.append("needs:")
        if "postgres" in needs:
            pg = needs["postgres"]
            lines += [
                "  postgres:",
                f'    version: "{pg["version"]}"',
                f"    database: {pg['database']}",
                f"    env: {pg['env']}",
                f"    backup: {{ every: {backup_every}, keep: {backup_keep} }}",
            ]
        if "redis" in needs:
            rd = needs["redis"]
            lines.append("  redis:")
            if "version" in rd:
                lines.append(f'    version: "{rd["version"]}"')
            lines.append(f"    env: {rd['env']}")
    if result.secrets:
        lines.append("secrets: [" + ", ".join(result.secrets) + "]")
    return "\n".join(lines) + "\n", manifest


RELEASE_TEMPLATE = """# App release CI: a thin caller of sealedstack's reusable workflow (spec 4.3).
# Replace <owner> with the account that owns the sealedstack repo, and keep the
# version pinned to the sealedstack release you run (sealedstack.version).
name: release

on:
  push:
    branches: [main] # build: mint the next version, build and push images
  workflow_dispatch: # release: deploy an existing version
    inputs:
      tag:
        description: "version to release, e.g. v0.0.12"
        required: true

jobs:
  sealedstack:
    uses: <owner>/sealedstack/.github/workflows/app.yml@v<version>
    with:
      event: ${{ github.event_name }}
      tag: ${{ inputs.tag }}
    secrets: inherit
    permissions:
      contents: write
      packages: write
      id-token: write
      actions: write
"""


def render_release_workflow(tool_owner: str, version: str) -> str:
    return RELEASE_TEMPLATE.replace("<owner>", tool_owner).replace("<version>", version)


def generate(
    files: Mapping[str, str],
    *,
    name: str,
    repo: str,
    registry_prefix: str,
    tool_owner: str,
    version: str,
    web_service: str = "",
    web_port: int = 0,
    health: str = "",
) -> Plan:
    """The onboarding change set for an app repo (``files``: path -> text)."""
    if MANIFEST_PATH in files:
        raise OnboardError(f"{repo} already has {MANIFEST_PATH}; nothing to onboard")
    compose_path = next((c for c in COMPOSE_CANDIDATES if c in files), "")
    if not compose_path:
        raise OnboardError(
            f"no compose file found in {repo} (looked for {', '.join(COMPOSE_CANDIDATES)}); "
            "add one, or onboard an image-only app by hand"
        )
    slug = repo.rsplit("/", 1)[-1]
    result = transform_compose(
        files[compose_path],
        name=name,
        repo_slug=slug,
        registry_prefix=registry_prefix,
        web_service=web_service,
        web_port=web_port,
        health=health,
    )
    manifest_text, manifest = render_manifest(name, compose_path, result)

    out: dict[str, str] = {MANIFEST_PATH: manifest_text, compose_path: result.text}
    notes = list(result.notes)
    changes = list(result.changes)

    needs_override = bool(result.removed.ports or result.removed.services or result.removed.env)
    override_note = ""
    if needs_override:
        if OVERRIDE_PATH in files:
            notes.append(
                f"`{OVERRIDE_PATH}` already exists and was left unchanged; review it, because the "
                "ports and database were removed from the base compose file"
            )
        else:
            out[OVERRIDE_PATH] = render_override(result, files[compose_path])
            override_note = (
                "local development only; it restores the ports"
                + (" and the database" if result.removed.services else "")
                + " for `docker compose up`, and sealedstack ignores it"
            )

    deletes = [path for path in REPLACED_WORKFLOWS if path in files]
    # scripts that only the replaced workflow used
    remaining_text = "\n".join(
        text for path, text in files.items() if path not in deletes and path.startswith(".github/workflows/")
    )
    for path in deletes:
        for script in re.findall(r"(?:\./)?(scripts/[\w./-]+\.sh)", files[path]):
            if script in files and script not in remaining_text and script not in deletes:
                deletes.append(script)
    out[RELEASE_WORKFLOW] = render_release_workflow(tool_owner, version)

    plan = Plan(files=out, deletes=deletes, changes=changes, manifest=manifest, notes=notes)
    plan.body = render_pr_body(
        name=name,
        plan=plan,
        compose_path=compose_path,
        result=result,
        override_note=override_note,
        replaced_existing_release=RELEASE_WORKFLOW in files,
    )
    return plan


def _needs_summary(needs: Needs, backup_every: str = "6h") -> str:
    parts = []
    if needs.postgres:
        parts.append(f"Postgres {needs.postgres['version']} with {_every_words(backup_every)} backups")
    if needs.redis:
        parts.append("Redis" + (f" {needs.redis['version']}" if needs.redis.get("version") else ""))
    return "; needs " + " and ".join(parts) if parts else ""


def _every_words(every: str) -> str:
    amount, unit = every[:-1], every[-1]
    word = {"m": "minute", "h": "hour", "d": "day"}[unit]
    return f"{amount}-{word}ly" if amount != "1" else {"m": "minutely", "h": "hourly", "d": "daily"}[unit]


def render_pr_body(
    *,
    name: str,
    plan: Plan,
    compose_path: str,
    result: ComposeResult,
    override_note: str,
    replaced_existing_release: bool,
) -> str:
    web = plan.manifest["web"]
    health = f", health {web['health']}" if "health" in web else ""
    lines = [f"This PR prepares {name} for sealedstack. Review each change:", ""]
    lines.append(
        f"1. `{MANIFEST_PATH}` (new): web = {web['service']}:{web['port']}{health}{_needs_summary(result.needs)}"
    )
    lines.append(f"2. `{compose_path}`:")
    lines += [f"   - {change}" for change in plan.changes]
    third = 3
    if override_note:
        lines.append(f"{third}. `{OVERRIDE_PATH}` (new): {override_note}")
        third += 1
    replaces = ", ".join(f"`{p.rsplit('/', 1)[-1]}`" if p.startswith(".github/") else f"`{p}`" for p in plan.deletes)
    release = f"{third}. `{RELEASE_WORKFLOW}` ({'replaced' if replaced_existing_release else 'new'}): build on merge; manual Release"
    if plan.deletes:
        release += f"; replaces {replaces} (deleted in this PR)"
    lines.append(release)
    if plan.notes:
        lines += ["", "Things to check:"]
        lines += [f"- {note}" for note in plan.notes]
    lines += [
        "",
        "Local development is unchanged: `docker compose up --build` still works"
        + (f" (`{OVERRIDE_PATH}` supplies the ports and database)." if override_note else "."),
        "",
        "**Merge this PR first.** The infra-repo PR that places this app waits for it.",
    ]
    return "\n".join(lines) + "\n"
