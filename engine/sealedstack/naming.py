"""One name per app, and everything named after it (D-089).

The app's name is its ``config/apps.yaml`` key, by default the app repo's name. OpenTofu keys every resource by
it, and from it come the Dokploy project and compose, the secrets provider and its Doppler project (D-088), the
default hostname ``<name>.<domain>``, and the image names ``<repo>-<service>`` without the repo repeated.

``derived`` lists those names (the add-app PR shows them); ``deviations`` lists where an existing app differs and
what changing it would touch, for the ``config.names.<app>`` rows of ``check``. Nothing is renamed automatically:
renaming the key recreates the app's Dokploy project, tunnel and database.
"""
from __future__ import annotations

import re
from typing import Mapping, Optional

import yaml


def app_name_from_repo(repo: str) -> str:
    """``owner/My_App`` -> ``my-app``: the repo name in the form apps.yaml keys allow."""
    slug = str(repo).rsplit("/", 1)[-1].lower()
    return re.sub(r"[^a-z0-9]+", "-", slug).strip("-")


def image_repo(repo: str, service: str) -> str:
    """The image name for a service; the same rule as ``scripts/lint_compose.py: image_repo``."""
    if service == repo or service.startswith(f"{repo}-"):
        return service
    return f"{repo}-{service}"


def default_hostname(name: str, domain: str) -> str:
    return f"{name}.{domain}" if domain else ""


def derived(name: str, *, repo: str = "", domain: str = "", provider: str = "") -> list[tuple[str, str]]:
    """``(what, name)`` rows for an app called ``name``."""
    provider = provider or name
    rows = [
        ("Dokploy project and compose", name),
        ("Tunnel", f"ss-{name}"),
        ("Secrets provider and Doppler project", provider),
    ]
    if domain:
        rows.append(("Default hostname", default_hostname(name, domain)))
    if repo:
        rows.append(("Images", f"{repo.rsplit('/', 1)[-1]}-<service> (no repeated prefix)"))
    return rows


def _images(deploy_compose: str) -> dict[str, str]:
    try:
        services = (yaml.safe_load(deploy_compose or "") or {}).get("services") or {}
    except yaml.YAMLError:
        return {}
    return {str(k): str((v or {}).get("image") or "") for k, v in services.items() if isinstance(v, dict)}


def deviations(name: str, rendered: Mapping, deploy_compose: str = "") -> list[tuple[str, str]]:
    """``(level, text)`` for each place an app differs from the rule; level ``warn`` or ``info``."""
    found: list[tuple[str, str]] = []
    manifest = rendered.get("manifest") or {}
    manifest_name = str(manifest.get("name") or "")
    if manifest_name and manifest_name != name:
        found.append((
            "warn",
            f"sealedstack.yaml says name '{manifest_name}' but the app is '{name}' in apps.yaml; sealedstack uses "
            f"'{name}' everywhere, so set name: {name} in the app repo's sealedstack.yaml",
        ))
    repo = str((rendered.get("source") or {}).get("repo") or "")
    if repo and app_name_from_repo(repo) != name:
        found.append((
            "info",
            f"the repo {repo} would give the name '{app_name_from_repo(repo)}'; keep '{name}' (renaming the apps.yaml "
            "key recreates the Dokploy project, tunnel and database)",
        ))
    provider = str((rendered.get("vault") or {}).get("provider") or "")
    if provider and provider != name:
        found.append((
            "info",
            f"secrets provider '{provider}' is not named after the app; to follow the rule, drop secrets.provider "
            f"(it defaults to '{name}') and the next apply creates or attaches the provider and Doppler project '{name}'",
        ))
    slug = repo.rsplit("/", 1)[-1] if repo else ""
    for service, image in sorted(_images(deploy_compose).items()):
        if slug and service.startswith(f"{slug}-") and f"/{slug}-{service}:" in image:
            found.append((
                "info",
                f"image of service '{service}' repeats the repo name ({slug}-{service}); the rule is "
                f"'{image_repo(slug, service)}': change it in the app's compose, and the next release pushes under the new name",
            ))
    return found


def access_default(stack: Mapping) -> Optional[str]:
    """The gate policy to use when add-app is not told one: the only policy, if there is exactly one."""
    policies = list(((stack.get("gate") or {}).get("policies") or {}).keys())
    return policies[0] if len(policies) == 1 else None
