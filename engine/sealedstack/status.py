"""Native ``status`` (spec 6.4): health, versions, apps and drift, as markdown
or ``--json``. Read-only. The pinned Status *issue* is written by the forge
adapter (M7); this computes and renders the content it would post.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from . import config as cfg
from . import preflight


def _summary(config: cfg.Config) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    slots = config.stack.get("slots") or {}
    rows.append(("Infra", config.name or "(unnamed)"))
    rows.append(("Domain", str(config.stack.get("domain", ""))))
    rows.append(("Slots", ", ".join(f"{k}={v}" for k, v in sorted(slots.items()))))
    rows.append(("Servers", ", ".join(sorted((config.servers.get("servers") or {}).keys())) or "(none)"))
    apps = config.apps.get("apps") or {}
    rows.append(("Apps", ", ".join(sorted(apps.keys())) or "(none)"))
    for name, app in sorted(apps.items()):
        hosts = ", ".join(app.get("hostnames", []))
        rows.append((f"app:{name}", f"{hosts} · access={app.get('access', '')}"))
    return rows


def render_markdown(config: cfg.Config, engine_version: str, health: list) -> str:
    lines = ["# sealedstack status", ""]
    lines.append(f"engine `{engine_version}`")
    lines.append("")
    lines.append("| | |")
    lines.append("|---|---|")
    for key, value in _summary(config):
        lines.append(f"| {key} | {value} |")
    lines.append("")
    lines.append("## Health")
    lines.append("")
    lines.append("| | Area | Check | Detail |")
    lines.append("|---|---|---|---|")
    icon = {"ok": "✅", "warn": "🟡", "fail": "❌", "skip": "⏳"}
    for result in health:
        lines.append(
            f"| {icon.get(result.status, '?')} | {result.area} | `{result.id}` | {result.detail} |"
        )
    return "\n".join(lines)


def render_json(config: cfg.Config, engine_version: str, health: list) -> str:
    return json.dumps(
        {
            "command": "status",
            "engine_version": engine_version,
            "infra": config.name,
            "domain": config.stack.get("domain", ""),
            "slots": config.stack.get("slots", {}),
            "servers": sorted((config.servers.get("servers") or {}).keys()),
            "apps": sorted((config.apps.get("apps") or {}).keys()),
            "health": [
                {"id": r.id, "area": r.area, "status": r.status, "detail": r.detail}
                for r in health
            ],
        },
        indent=2,
    )


def run(
    *,
    code_root,
    env: Optional[dict] = None,
    http=None,
    secrets=None,
    tcp_probe=None,
    json_output: bool = False,
    stream=None,
) -> int:
    import os

    env = dict(os.environ) if env is None else dict(env)
    code_root = code_root if code_root is not None else cfg.find_code_root(env)
    out = stream if stream is not None else sys.stdout
    config = cfg.load_config(cfg.resolve_config_dir(None, env, code_root), code_root, env)
    version = cfg.read_version(code_root)
    ctx = preflight.build_context(
        config, env=env, http=http, secrets=secrets, version=version, tcp_probe=tcp_probe
    )
    health = preflight.run_checks(ctx)
    if json_output:
        print(render_json(config, version, health), file=out)
    else:
        print(render_markdown(config, version, health), file=out)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="sealedstack status")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--config", default="")
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    return run(code_root=None, json_output=args.json)
