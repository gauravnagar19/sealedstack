"""A repo app is never deployed by `tofu apply` (live run 2026-11-01).

The provider's `deploy_on_change` defaults to true, so creating the compose of an app that has not been released yet pulled
`image:${APP_VERSION}` with no version and failed the whole platform apply ("deployment finished with status error").
"""
from __future__ import annotations

from test_engine_server import REPO


def test_the_repo_app_compose_is_not_deployed_on_create():
    text = (REPO / "tofu" / "modules" / "platform_dokploy" / "main.tf").read_text(encoding="utf-8")
    block = text[text.index('resource "dokploy_compose" "app"'):]
    block = block[: block.index("\n}\n") + 3]
    code = "\n".join(line for line in block.splitlines() if not line.lstrip().startswith("#"))
    assert "deploy_on_change = false" in code


def _block(text: str, header: str) -> str:
    block = text[text.index(header):]
    return block[: block.index("\n}\n") + 3]


def test_app_and_image_composes_ignore_env_drift_so_apply_never_erases_database_url_or_the_version():
    """Live run 2026-11-01: the second plan showed `env = DATABASE_URL, APP_VERSION -> null` on the app compose."""
    text = (REPO / "tofu" / "modules" / "platform_dokploy" / "main.tf").read_text(encoding="utf-8")
    assert "ignore_changes = [raw, git, env]" in _block(text, 'resource "dokploy_compose" "app"')
    assert "ignore_changes = [env]" in _block(text, 'resource "dokploy_compose" "image"')
