"""``verify``: does the running system look the way the config says? Read-only, a few seconds.

``check`` asks whether the credentials and config are in order; nothing asked the live system until a human did it by
hand after the first rebuild (live run 2026-11-01). Every probe here is one request or one TCP connect with a short
timeout, and they all run at once, so the whole step costs about as much as its slowest probe.

* each app hostname answers the way its gate says (Access redirect for a gated app, a normal answer for a public one);
* a path from ``edge_security.blocked_paths`` is refused at the edge (403);
* the public IP of each server has nothing listening on SSH, HTTP, HTTPS, the Dokploy panel or Postgres;
* each app's compose in Dokploy finished its last deploy.
"""
from __future__ import annotations

import socket
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from . import config as config_mod
from . import dokploy as dokploy_mod
from . import secrets as secrets_mod

PUBLIC_PORTS = (22, 80, 443, 3000, 5432)
# A passing run waits the full CONNECT_TIMEOUT once (a filtered port never answers), so it is the floor of the step; every
# probe runs at once, so the step costs about as much as its slowest probe. An open port answers in milliseconds.
HTTP_TIMEOUT = 4.0
CONNECT_TIMEOUT = 2.0
DEPLOY_TIMEOUT = 8.0
EDGE_RETRY_AFTER = 2.0  # a hostname just created by `apply` can take a moment to answer; only a failed probe waits


@dataclass
class Probe:
    id: str
    status: str  # ok | fail | skip
    detail: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - urllib hook
        return None


def fetch(url: str, timeout: float = HTTP_TIMEOUT) -> tuple[int, str]:
    """(status, Location) of one GET, redirects not followed. (0, reason) when nothing answered."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "sealedstack-verify"})
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.headers.get("Location", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", "") if exc.headers else ""
    except Exception as exc:  # noqa: BLE001 - the reason is the detail
        return 0, str(exc)


def connects(host: str, port: int, timeout: float = CONNECT_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _front_ok(gated: bool, status: int, location: str) -> bool:
    if gated:
        return status in (301, 302, 303, 307) and "cloudflareaccess.com" in location
    return 200 <= status < 400


def _twice(fetcher: Callable, url: str, good: Callable[[int, str], bool]) -> tuple[int, str]:
    """One GET; a failed answer is asked again once after a short wait, a good one never waits."""
    status, location = fetcher(url)
    if good(status, location):
        return status, location
    time.sleep(EDGE_RETRY_AFTER)
    return fetcher(url)


def _blocked_probe_path(config: config_mod.Config) -> str:
    """One literal path from edge_security.blocked_paths (a prefix such as /.env), or ''."""
    for path in (config.stack.get("edge_security") or {}).get("blocked_paths") or []:
        path = str(path)
        if path.startswith("/") and "*" not in path:
            return path
    return ""


def edge_jobs(config: config_mod.Config, fetcher: Callable) -> list[Callable[[], Probe]]:
    jobs: list[Callable[[], Probe]] = []
    blocked = _blocked_probe_path(config)
    for app, spec in sorted((config.apps.get("apps") or {}).items()):
        if spec.get("enabled") is False:
            continue
        gated = str(spec.get("access") or "public") != "public"
        for host in spec.get("hostnames") or []:
            def front(app=app, host=host, gated=gated):
                status, location = _twice(fetcher, f"https://{host}/", lambda s, loc: _front_ok(gated, s, loc))
                ok = _front_ok(gated, status, location)
                want = "redirects to the Cloudflare Access login" if gated else "answers"
                return Probe(f"edge.{host}", "ok" if ok else "fail",
                             f"{host} {want}" if ok else f"{host} should {want}, got {status or 'no answer'} {location}".strip())

            jobs.append(front)
            if blocked:
                def refused(host=host, blocked=blocked):
                    status, _ = fetcher(f"https://{host}{blocked}")
                    return Probe(f"edge.{host}.blocked", "ok" if status == 403 else "fail",
                                 f"{host}{blocked} is refused at the edge (403)" if status == 403
                                 else f"{host}{blocked} should be refused with 403, got {status or 'no answer'}")

                jobs.append(refused)
    return jobs


def port_jobs(config: config_mod.Config, source: secrets_mod.SecretSource, connector: Callable) -> list[Callable[[], Probe]]:
    jobs: list[Callable[[], Probe]] = []
    for server in sorted((config.servers.get("servers") or {})):
        ip_name, _pw = config.bootstrap_secrets(server)
        found = source.get(ip_name)
        if not found.found:
            jobs.append(lambda server=server, ip_name=ip_name: Probe(
                f"ports.{server}", "skip", f"{ip_name} is not in the secret source, so the public ports were not probed"))
            continue
        for port in PUBLIC_PORTS:
            def closed(server=server, ip=found.value, port=port):
                open_ = connector(ip, port)
                return Probe(f"ports.{server}.{port}", "fail" if open_ else "ok",
                             f"{server}: public TCP {port} is OPEN" if open_ else f"{server}: public TCP {port} closed")

            jobs.append(closed)
    return jobs


def deploy_jobs(config: config_mod.Config, client) -> list[Callable[[], Probe]]:
    """One probe: every app's compose reports its last deploy as done."""
    names = {name for name, spec in (config.apps.get("apps") or {}).items() if spec.get("enabled") is not False}
    if not names or client is None:
        return []

    def deployed() -> Probe:
        try:
            projects = client.get("/api/project.all") or []
        except Exception as exc:  # noqa: BLE001
            return Probe("deploy", "fail", f"could not read the Dokploy projects: {exc}")
        states: dict[str, list[str]] = {}
        for project in projects:
            for environment in project.get("environments") or [{"compose": project.get("compose") or []}]:
                for compose in environment.get("compose") or []:
                    if compose.get("name") in names:
                        states.setdefault(compose["name"], []).append(str(compose.get("composeStatus") or ""))
        bad = [f"{name}: {', '.join(states.get(name) or ['missing'])}" for name in sorted(names)
               if not states.get(name) or any(state != "done" for state in states[name])]
        if bad:
            return Probe("deploy", "fail", "an app is not in the done state in Dokploy (" + "; ".join(bad) + ")")
        return Probe("deploy", "ok", "every app's last deploy is done in Dokploy (" + ", ".join(sorted(names)) + ")")

    return [deployed]


def run(
    config: config_mod.Config,
    env: Optional[Mapping[str, str]] = None,
    *,
    source: Optional[secrets_mod.SecretSource] = None,
    client=None,
    fetcher: Callable = fetch,
    connector: Callable = connects,
    out=None,
) -> int:
    out = out if out is not None else sys.stdout
    env = dict(env or {})
    source = source or secrets_mod.build_source(config.stack, config.config_dir, env)
    if client is None:
        try:
            client = dokploy_mod.from_config(config, env, source)
            client.timeout = DEPLOY_TIMEOUT
        except Exception:  # noqa: BLE001 - reported by the deploy probe below
            client = None
    jobs = edge_jobs(config, fetcher) + port_jobs(config, source, connector) + deploy_jobs(config, client)
    with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), 16))) as pool:
        probes = list(pool.map(lambda job: job(), jobs))
    for probe in probes:
        icon = {"ok": "✅", "fail": "❌", "skip": "⏳"}[probe.status]
        print(f"verify: {icon} {probe.id}: {probe.detail}", file=out)
    failed = [probe for probe in probes if probe.status == "fail"]
    print(f"verify: {len(probes) - len(failed)} of {len(probes)} probes ok" + (f", {len(failed)} failed" if failed else ""), file=out)
    return 1 if failed else 0


def main(argv: Optional[list] = None) -> int:
    import os

    env = dict(os.environ)
    config = config_mod.load_config(env=env)
    try:
        return run(config, env)
    except config_mod.ConfigError as exc:
        print(f"sealedstack: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
