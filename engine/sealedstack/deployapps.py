"""``deploy-apps`` in Python (H11, D-047).

Trigger a Dokploy deploy for each app whose compose/env changed, then poll until
each deploy finishes. Replaces ``scripts/deploy-apps.sh``.

The OpenTofu provider creates composes with ``auto_deploy=false``; nothing starts
until an explicit deploy. ``build.mode: ci`` apps (D-030) are skipped: their image
does not exist until the release workflow builds it. A rebuilt server gets new
compose ids, so the deploy is never skipped (N5, N6).
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import envfile
from . import platformstate as platformstate_mod
from . import secrets as secrets_mod


class DeployError(RuntimeError):
    pass


def app_hash(rendered: dict, env_map: Mapping[str, str], ids: list[str]) -> str:
    trigger = {
        key: rendered.get(key) for key in ("source", "servers", "hostnames", "web", "needs", "_compose_hash")
    }
    payload = (
        json.dumps(trigger, sort_keys=True, separators=(",", ":"))
        + "\n"
        + json.dumps(dict(env_map), sort_keys=True, separators=(",", ":"))
        + "\n"
        + "\n".join(ids)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def set_app_version(env_text: str, version: str) -> str:
    found = False
    out: list[str] = []
    for line in env_text.split("\n"):
        if line.startswith("APP_VERSION="):
            out.append("APP_VERSION=" + version)
            found = True
        else:
            out.append(line)
    if not found:
        out.append("APP_VERSION=" + version)
    return "\n".join(out)


def deploy_ids(client: dokploy_mod.DokployClient, compose_id: str) -> list[str]:
    return [str(entry.get("deploymentId")) for entry in client.deployments(compose_id) or []]


def last_deploy_status(client: dokploy_mod.DokployClient, compose_id: str) -> str:
    entries = sorted(client.deployments(compose_id) or [], key=lambda e: str(e.get("createdAt") or ""))
    if not entries:
        return ""
    return str(entries[-1].get("status") or "")


def poll_deploy(
    client: dokploy_mod.DokployClient,
    compose_id: str,
    previous_ids: list[str],
    *,
    sleep=time.sleep,
    tries: int = 180,
    interval: int = 5,
) -> str:
    seen = set(previous_ids)
    new_id = ""
    for _ in range(tries):
        try:
            entries = client.deployments(compose_id) or []
        except Exception:  # noqa: BLE001 - transient; keep polling
            sleep(interval)
            continue
        fresh = [e for e in entries if str(e.get("deploymentId")) not in seen]
        fresh.sort(key=lambda e: str(e.get("createdAt") or ""))
        if not fresh:
            sleep(interval)
            continue
        new_id = str(fresh[-1].get("deploymentId"))
        status = str(next((e.get("status") for e in entries if str(e.get("deploymentId")) == new_id), ""))
        if status == "done":
            return f"{new_id}:done"
        if status in ("error", "unknown", "timeout"):
            raise DeployError(f"{new_id}:{status}")
        sleep(interval)
    raise DeployError(f"{new_id or 'unknown'}:timeout")


def released_version(app: str, repo: str, env: Mapping[str, str]) -> str:
    upper = "".join(c if c.isalnum() else "_" for c in app.upper())
    per_app = env.get(f"SEALEDSTACK_RELEASED_VERSION_{upper}")
    if per_app:
        return per_app
    return env.get("SEALEDSTACK_RELEASED_VERSION", "")


def deployed_version(client: dokploy_mod.DokployClient, ids: list[str]) -> str:
    for compose_id in ids:
        try:
            env_text = client.compose_env(str(compose_id))
        except Exception:  # noqa: BLE001
            continue
        for line in env_text.split("\n"):
            if line.startswith("APP_VERSION="):
                return line[len("APP_VERSION=") :]
    return ""


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    force: bool = False,
    apps: Optional[list[str]] = None,
    client: Optional[dokploy_mod.DokployClient] = None,
    state: Optional[platformstate_mod.PlatformState] = None,
    source: Optional[secrets_mod.SecretSource] = None,
    sleep=time.sleep,
    out=None,
) -> None:
    env = dict(env or {})
    out = out if out is not None else sys.stdout
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    state = state or platformstate_mod.PlatformState(config, env, source)
    client = client or dokploy_mod.from_config(config, env, source)

    build = Path(config.code_root) / ".build"
    marker_dir = build / "deploy"
    marker_dir.mkdir(parents=True, exist_ok=True)
    files = sorted((build / "apps").glob("*.json"))
    if not files:
        print("deploy-apps: no rendered apps; nothing to deploy", file=out)
        return

    compose_ids = state.output("compose_ids") or {}
    env_map = envfile.load(envfile.env_path(config.code_root))

    for path in files:
        rendered = json.loads(path.read_text(encoding="utf-8") or "{}")
        app = str(rendered.get("name") or path.stem)
        if apps and app not in apps:
            continue
        marker = marker_dir / f"{app}.sha256"
        ids = [str(v) for v in (compose_ids.get(app) or {}).values()]
        if not ids:
            print(f"==> {app}: no compose to deploy", file=out)
            continue

        source_image = (rendered.get("source") or {}).get("image") or ""
        build_mode = ((rendered.get("manifest") or {}).get("build") or {}).get("mode") or "ci"
        if not source_image and build_mode == "ci":
            repo = (rendered.get("source") or {}).get("repo") or ""
            released = released_version(app, repo, env) or deployed_version(client, ids)
            if not released:
                print(
                    f"==> {app}: build.mode is 'ci' and no released version is recorded; "
                    "run the release workflow",
                    file=out,
                )
                continue
            print(f"==> {app}: redeploying recorded version {released}", file=out)
            for compose_id in ids:
                if not last_deploy_status(client, compose_id):
                    client.compose_update(compose_id, set_app_version(client.compose_env(compose_id), released))

        digest = app_hash(rendered, env_map.get(app) or {}, ids)
        needs_deploy = any(last_deploy_status(client, cid) != "done" for cid in ids)

        if not force and not needs_deploy and marker.exists() and marker.read_text() == digest:
            print(f"==> {app}: unchanged; skipping deploy", file=out)
            continue

        print(f"==> {app}: deploying", file=out)
        for compose_id in ids:
            previous = deploy_ids(client, compose_id)
            client.compose_deploy(compose_id)
            result = poll_deploy(client, compose_id, previous, sleep=sleep)
            print(f"    {compose_id}: {result}", file=out)

        marker.write_text(digest, encoding="utf-8")
