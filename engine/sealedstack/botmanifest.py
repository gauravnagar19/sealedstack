"""The GitHub App manifest flow (M7, spec §9.3).

The bot is a **user-owned GitHub App** created with one click: the manifest is
POSTed to GitHub, which creates the App and (via the redirect) hands back the
credentials. The engine builds the manifest + the one-click form; the values are
stored in the secret source as ``SEALEDSTACK_BOT_*`` (D-050).
"""
from __future__ import annotations

import html
import json

BOT_NAME = "sealedstack-bot"

# What the bot identity needs on the infra repo (D-023): read the repo, open the
# Setup/app PRs, comment on and update the PRs and the pinned Status issue, and
# publish app-repo variables/secrets. Nothing else.
PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "actions": "write",
    "actions_variables": "write",
    "secrets": "write",
    "environments": "write",
    "workflows": "write",  # version-bump PRs edit .github/workflows/sealedstack.yml
    "administration": "read",
    "metadata": "read",
}

EVENTS = ["pull_request", "issue_comment", "workflow_dispatch"]


def manifest(name: str, homepage: str, redirect_url: str, *, webhook_url: str = "") -> dict:
    return {
        "name": name or BOT_NAME,
        "url": homepage,
        "hook_attributes": {"url": webhook_url, "active": bool(webhook_url)},
        "redirect_url": redirect_url,
        "public": False,
        "default_permissions": PERMISSIONS,
        "default_events": EVENTS,
    }


def registration_url(name: str = BOT_NAME, homepage: str = "", *, org: str = "") -> str:
    """A GitHub link that opens the "register a GitHub App" form already filled in.

    GitHub accepts the App's name, description, URL, permissions and events as
    query parameters, so this works from a pull-request comment (a link cannot
    POST the manifest). The user still clicks "Create GitHub App", then generates a
    private key and installs the App on the infra repository.
    """
    from urllib.parse import urlencode

    base = f"https://github.com/organizations/{org}/settings/apps/new" if org else "https://github.com/settings/apps/new"
    params = [
        ("name", name or BOT_NAME),
        ("description", "Opens sealedstack's pull requests and posts its plan and checklist comments."),
        ("url", homepage or "https://github.com/"),
        ("public", "false"),
        ("webhook_active", "false"),
    ]
    params += [(perm, level) for perm, level in PERMISSIONS.items() if perm != "metadata"]
    return f"{base}?{urlencode(params)}"


def form_html(name: str, homepage: str, redirect_url: str, *, state: str = "", org: str = "") -> str:
    """A self-submitting HTML page that POSTs the manifest to GitHub."""
    target = f"https://github.com/organizations/{org}/settings/apps/new" if org else "https://github.com/settings/apps/new"
    if state:
        target += f"?state={html.escape(state)}"
    payload = html.escape(json.dumps(manifest(name, homepage, redirect_url)))
    return (
        "<!doctype html><html><body>\n"
        f'<form action="{target}" method="post">\n'
        f'  <input type="hidden" name="manifest" value="{payload}">\n'
        "  <button type=\"submit\">Create the sealedstack-bot app</button>\n"
        "</form>\n</body></html>\n"
    )
