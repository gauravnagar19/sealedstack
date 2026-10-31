"""What the bot identity needs on GitHub (D-058).

For now the bot identity is a **fine-grained personal access token**
(``GH_TOKEN``), limited to the infra repository and the onboarded app
repositories. This module is the single list of the permissions it needs, derived
from the forge calls the engine makes: ``CALLS`` maps every endpoint
``GitHubForge`` requests to the permission GitHub requires for it, and a test
(``tests/pytest/test_bot_permissions.py``) parses ``forge.py`` so a new call
cannot be added without being mapped here.

The permission names and levels were checked against GitHub's table of
fine-grained token permissions per endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode

TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"


@dataclass(frozen=True)
class Permission:
    name: str  # the name shown on GitHub's token form
    param: str  # the query-parameter name for the pre-filled form
    access: str  # read | write
    why: str
    optional: bool = False


# Repository permissions, in the order the token form lists them.
PERMISSIONS: tuple[Permission, ...] = (
    Permission("Metadata", "metadata", "read", "Mandatory for every token; also used to look up a user's repository role (comment-command and approver checks)."),
    Permission("Contents", "contents", "write", "Read files and branches, and write the Setup, onboarding and version-bump branches."),
    Permission("Pull requests", "pull_requests", "write", "Open the Setup, onboarding and upgrade PRs; read PRs for the plan, approval and merge checks."),
    Permission("Issues", "issues", "write", "The pinned Status issue, the living checklist and plan comments (PR comments go through the Issues API)."),
    Permission("Variables", "actions_variables", "write", "Publish the deploy settings (compose ids, Dokploy URL, release trust ids, registry) to each app repo."),
    Permission("Deployments", "deployments", "read", "Read each app repo's latest successful `production` deployment, the released version recorded by the app release workflow (D-060), to redeploy it after a server rebuild."),
    Permission("Secrets", "secrets", "write", "Store the Dokploy API key as a repository secret in each app repo (the release job reads it; environment secrets are not available to private repos on GitHub Free)."),
    Permission("Workflows", "workflows", "write", "PRs that add or change `.github/workflows/*` files (the app release workflow, version-bump PRs) are refused without it."),
    Permission(
        "Administration", "administration", "write",
        "Only to let sealedstack create the `production` environment itself. Skip it and create that environment by hand in each app repo.",
        optional=True,
    ),
)

# Every endpoint GitHubForge requests -> (permission name, access). Paths are
# normalised: each ``{...}`` is ``{}`` and the query string is dropped.
# The secret calls are repository-level (FR-2), hence ``Secrets``.
CALLS: dict[tuple[str, str], tuple[str, str]] = {
    ("GET", "/repos/{}/contents/{}"): ("Contents", "read"),
    ("PUT", "/repos/{}/contents/{}"): ("Contents", "write"),
    ("DELETE", "/repos/{}/contents/{}"): ("Contents", "write"),
    ("GET", "/repos/{}/git/ref/heads/{}"): ("Contents", "read"),
    ("POST", "/repos/{}/git/refs"): ("Contents", "write"),
    ("GET", "/repos/{}/branches/{}"): ("Contents", "read"),
    ("POST", "/repos/{}/pulls"): ("Pull requests", "write"),
    ("GET", "/repos/{}/pulls/{}"): ("Pull requests", "read"),
    ("GET", "/repos/{}/pulls"): ("Pull requests", "read"),
    ("GET", "/repos/{}/commits/{}/pulls"): ("Pull requests", "read"),
    ("GET", "/repos/{}/issues"): ("Issues", "read"),
    ("POST", "/repos/{}/issues"): ("Issues", "write"),
    ("PATCH", "/repos/{}/issues/{}"): ("Issues", "write"),
    ("GET", "/repos/{}/issues/{}/comments"): ("Issues", "read"),
    ("POST", "/repos/{}/issues/{}/comments"): ("Issues", "write"),
    ("PATCH", "/repos/{}/issues/comments/{}"): ("Issues", "write"),
    ("POST", "/repos/{}/actions/variables"): ("Variables", "write"),
    ("PATCH", "/repos/{}/actions/variables/{}"): ("Variables", "write"),
    ("GET", "/repos/{}/actions/variables/{}"): ("Variables", "read"),
    ("PUT", "/repos/{}/environments/{}"): ("Administration", "write"),
    ("GET", "/repos/{}/actions/secrets/public-key"): ("Secrets", "write"),
    ("PUT", "/repos/{}/actions/secrets/{}"): ("Secrets", "write"),
    ("GET", "/repos/{}/actions/secrets/{}"): ("Secrets", "read"),
    ("GET", "/repos/{}/collaborators/{}/permission"): ("Metadata", "read"),
    ("GET", "/repos/{}/deployments"): ("Deployments", "read"),
    ("GET", "/repos/{}/deployments/{}/statuses"): ("Deployments", "read"),
}

# GitHub calls made outside ``GitHubForge`` (kept here so the list is complete).
OTHER_CALLS: tuple[tuple[str, str], ...] = (
    ("scripts/render.py: GET /repos/{repo}/contents/... (app manifests)", "Contents read"),
    ("scripts/validate.py: GET /repos/{repo}", "Metadata read"),
    ("app.yml creates a deployment and its status in the APP repo with the built-in GITHUB_TOKEN (`deployments: write` in the caller); no bot permission", "none"),
)

REPOSITORY_ACCESS = "Only select repositories: the infra repository plus each onboarded app repository."


def required() -> list[Permission]:
    return [p for p in PERMISSIONS if not p.optional]


def optional() -> list[Permission]:
    return [p for p in PERMISSIONS if p.optional]


def short_list(*, include_optional: bool = False) -> str:
    """``Contents: read/write, Pull requests: read/write, ...`` for one line of text."""
    parts = []
    for p in PERMISSIONS:
        if p.optional and not include_optional:
            continue
        parts.append(f"{p.name}: {'read' if p.access == 'read' else 'read and write'}")
    return ", ".join(parts)


def table_markdown() -> str:
    lines = ["| Permission | Access | Why |", "|---|---|---|"]
    for p in PERMISSIONS:
        access = "Read" if p.access == "read" else "Read and write"
        name = f"{p.name} (optional)" if p.optional else p.name
        lines.append(f"| {name} | {access} | {p.why} |")
    return "\n".join(lines)


def token_url(repo: str = "", *, name: str = "sealedstack-bot") -> str:
    """The fine-grained token form with name, owner, expiry and permissions pre-filled.

    GitHub documents a query parameter for each form field; the permission
    parameter names follow GitHub's permission names. Anything GitHub does not
    recognise is simply ignored, so the table in the checklist stays the source
    of truth.
    """
    params: list[tuple[str, str]] = [
        ("name", name[:40]),
        ("description", "sealedstack bot identity (D-058): opens PRs, posts checklists and plan comments, publishes deploy settings."),
        ("expires_in", "90"),
    ]
    owner = repo.split("/", 1)[0] if "/" in repo else ""
    if owner:
        params.append(("target_name", owner))
    params += [(p.param, p.access) for p in required()]
    return f"{TOKEN_URL}?{urlencode(params)}"


def token_kind(token: str) -> str:
    """``fine-grained`` | ``classic`` | ``builtin`` (Actions/installation) | ``oauth`` | ``unknown``."""
    if token.startswith("github_pat_"):
        return "fine-grained"
    if token.startswith("ghp_"):
        return "classic"
    if token.startswith("ghs_"):
        return "builtin"
    if token.startswith(("gho_", "ghu_")):
        return "oauth"
    return "unknown"


def normalise(path: str) -> str:
    """``/repos/{repo}/issues?state=all`` -> ``/repos/{}/issues``."""
    import re

    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", path)


def lookup(method: str, path: str) -> Optional[tuple[str, str]]:
    return CALLS.get((method.upper(), normalise(path)))
