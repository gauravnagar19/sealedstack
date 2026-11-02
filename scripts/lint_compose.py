#!/usr/bin/env python3
"""Lint app compose files against sealedstack's rules (spec section 5.4, 4.2).

Rules:
  - No ``ports:`` on any service. Traffic arrives through Traefik on
    ``dokploy-network``.
  - No database service inside the compose file when that database is declared
    under ``needs:``.
  - No host-escape settings (D-063): privileged, the Docker socket or host paths,
    host network/pid/ipc, extra capabilities, devices, disabled confinement. The
    rules live in ``engine/sealedstack/composelint.py`` (one source, also embedded
    in the app release workflow); see ``lint_security``.
  - ``build.mode: ci`` (the default, D-030): every service with ``build:`` must
    also pin ``image: <registry>/<owner>/<repo>-<service>:${APP_VERSION}`` so the
    built image exists in the registry and Dokploy never builds. ``build.mode:
    server`` leaves ``build:`` open (Dokploy builds) and ``image:`` optional.
"""
from __future__ import annotations

import copy


def _services(compose: dict | None) -> dict:
    return (compose or {}).get("services") or {}


def image_repo(repo: str, service: str) -> str:
    """``<repo>-<service>``, without repeating the repo name (D-089): service ``paisa-web`` in repo ``paisa`` is
    ``paisa-web``, not ``paisa-paisa-web``, and a service named like the repo is just the repo."""
    if service == repo or service.startswith(f"{repo}-"):
        return service
    return f"{repo}-{service}"


def expected_image(registry: str, owner: str, repo: str, service: str) -> str:
    """The image ref a ``ci``-mode service must pin (spec 4.2)."""
    return f"{registry}/{owner}/{image_repo(repo, service)}:${{APP_VERSION}}"


def legacy_image(registry: str, owner: str, repo: str, service: str) -> str:
    """The form onboarding wrote before D-089 (``<repo>-<service>`` always); still accepted."""
    return f"{registry}/{owner}/{repo}-{service}:${{APP_VERSION}}"


def lint_security(compose: dict | None) -> list[str]:
    """Host-escape problems (D-063): refused by ``check``, ``plan`` and ``apply``."""
    import pathlib
    import sys

    engine = str(pathlib.Path(__file__).resolve().parent.parent / "engine")
    if engine not in sys.path:
        sys.path.insert(0, engine)
    from sealedstack import composelint

    return composelint.lint(compose or {})


def lint(compose: dict, needs: dict | None = None) -> list[str]:
    needs = needs or {}
    problems: list[str] = []
    services = _services(compose)

    for service, spec in services.items():
        if isinstance(spec, dict) and "ports" in spec:
            problems.append(
                f"service '{service}': has 'ports:' — route through Traefik instead"
            )

    if "postgres" in needs:
        for service, spec in services.items():
            image = str((spec or {}).get("image", ""))
            if "postgres" in image:
                problems.append(
                    f"service '{service}': postgres is declared under needs: — remove it from compose"
                )

    return problems


def lint_build(
    compose: dict | None,
    mode: str,
    *,
    registry: str = "",
    owner: str = "",
    repo: str = "",
) -> list[str]:
    """The per-mode build/image rule (spec 4.2).

    Only ``ci`` constrains the compose. In ``server`` mode ``build:`` is allowed
    and ``image:`` optional, so there is nothing to enforce here.
    """
    problems: list[str] = []
    if mode != "ci":
        return problems

    for service, spec in _services(compose).items():
        if not isinstance(spec, dict) or "build" not in spec:
            continue
        image = str(spec.get("image", ""))
        if not image:
            problems.append(
                f"service '{service}': build.mode is 'ci' so it must also set "
                f"image: <registry>/<owner>/<repo>-{service}:${{APP_VERSION}}"
            )
            continue
        if registry and owner and repo:
            want = expected_image(registry, owner, repo, service)
            if image not in (want, legacy_image(registry, owner, repo, service)):
                problems.append(
                    f"service '{service}': image '{image}' must be '{want}' "
                    "in build.mode 'ci'"
                )
    return problems


def strip_build(compose: dict | None) -> dict:
    """A copy of ``compose`` with every ``build:`` key removed (the deploy
    compose Dokploy pulls in ``ci`` mode; it never clones or builds)."""
    out = copy.deepcopy(compose or {})
    for spec in _services(out).values():
        if isinstance(spec, dict):
            spec.pop("build", None)
    return out


PLATFORM_NETWORK = "dokploy-network"


def database_consumers(compose: dict | None, manifest: dict | None) -> list[str]:
    """Services that read a platform-provided variable (``needs.*.env``, e.g. DATABASE_URL)."""
    names = {
        str(spec["env"])
        for spec in ((manifest or {}).get("needs") or {}).values()
        if isinstance(spec, dict) and spec.get("env")
    }
    found = []
    for service, spec in _services(compose or {}).items():
        if isinstance(spec, dict) and any(n in str(spec.get("environment") or "") + str(spec.get("env_file") or "") for n in names):
            found.append(service)
    return found


def shared_network_services(compose: dict | None, manifest: dict | None, web: dict | None = None) -> set[str]:
    """Service names an app puts on dokploy-network: its web service (Dokploy joins it) and the database consumers.

    Every container on that network answers to its service name, so two apps that both publish ``api`` there can be
    resolved to each other's container.
    """
    names = set(database_consumers(compose, manifest))
    service = str(((web or (manifest or {}).get("web")) or {}).get("service") or "")
    if service:
        names.add(service)
    return names


def attach_platform_network(compose: dict | None, manifest: dict | None) -> dict:
    """Join every service that reads a platform-provided variable (``needs.*.env``, e.g. DATABASE_URL) to dokploy-network.

    The managed database is a Swarm service on dokploy-network by name. Dokploy attaches only the service that has the
    domain to that network, so an API that reads DATABASE_URL could not resolve the host (live run 2026-11-01:
    ``getaddrinfo ENOTFOUND``). Services keep their default network too, so they still reach each other.
    """
    out = copy.deepcopy(compose or {})
    consumers = set(database_consumers(out, manifest))
    for service, spec in _services(out).items():
        if service not in consumers:
            continue
        nets = spec.get("networks")
        if nets is None:
            spec["networks"] = ["default", PLATFORM_NETWORK]
        elif isinstance(nets, list):
            if PLATFORM_NETWORK not in nets:
                nets.append(PLATFORM_NETWORK)
        elif isinstance(nets, dict):
            nets.setdefault(PLATFORM_NETWORK, None)
    if consumers:
        top = out.setdefault("networks", {})
        if isinstance(top, dict):
            top.setdefault(PLATFORM_NETWORK, {"external": True})
    return out
