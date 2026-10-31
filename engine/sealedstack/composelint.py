"""Host-escape lint for app compose files (D-063, FR-1 defence in depth).

An app's compose file runs on the server through Dokploy. Whoever can change it
can ask Docker for root on the host: a privileged container, the Docker socket,
a host path, the host's namespaces. This lint refuses those settings. It runs

- in the app release job (``app.yml``), on the rendered deploy compose, and
- in the infra repo's ``check`` / ``plan`` / ``apply``, on every app's compose.

Pure standard library, so the same source is embedded verbatim in ``app.yml``
(``tests/pytest/test_composelint.py`` keeps the two identical).
"""
from __future__ import annotations

import posixpath
import sys

# Host paths a container may bind-mount (read the time zone, nothing else).
ALLOWED_HOST_PATHS = ("/etc/localtime",)
# Capabilities an app may add. Everything else (SYS_ADMIN, NET_ADMIN, SYS_PTRACE, ALL, ...) is refused.
ALLOWED_CAPS = ("CHOWN", "FOWNER", "KILL", "NET_BIND_SERVICE", "SETGID", "SETUID")
# Namespace / confinement keys: ``host`` shares the host's view with the container.
HOST_NAMESPACE_KEYS = ("pid", "ipc", "uts", "userns_mode", "cgroup")
SOCKET_NAMES = ("docker.sock", "containerd.sock", "podman.sock", "crio.sock")


def _truthy(value) -> bool:
    """True for anything that is not a literal false (an unresolved ${VAR} counts as true)."""
    if value is None or value is False:
        return False
    return str(value).strip().lower() not in ("false", "no", "off", "0", "")


def _sources(volume) -> "list[tuple[str, str, bool]]":
    """``(source, target, is_bind)`` for one service volume entry (short or long syntax)."""
    if isinstance(volume, dict):
        kind = str(volume.get("type") or ("bind" if str(volume.get("source", "")).startswith(("/", ".", "~", "$")) else "volume"))
        if kind == "tmpfs":
            return []
        source = str(volume.get("source") or "")
        return [(source, str(volume.get("target") or ""), kind == "bind")]
    text = str(volume)
    parts = text.split(":")
    if len(parts) == 1:
        return []  # an anonymous volume: just a container path
    source, target = parts[0], parts[1]
    is_bind = source.startswith(("/", ".", "~", "$"))
    return [(source, target, is_bind)]


def _host_path_problem(source: str, target: str) -> str:
    """Why this bind source is refused ("" when it is fine)."""
    names = (source + " " + target).lower()
    if any(sock in names for sock in SOCKET_NAMES) or source.rstrip("/").endswith("/run") or source.rstrip("/") == "/var/run":
        return "mounts the container runtime socket, which is root on the host. Remove the mount"
    if source.startswith("$"):
        return "has a bind source that is only known at deploy time, so it cannot be checked. Use a named volume"
    if source.startswith("~"):
        return "bind-mounts a path in the host's home directory. Use a named volume"
    if source.startswith("/"):
        if posixpath.normpath(source) in ALLOWED_HOST_PATHS:
            return ""
        return f"bind-mounts the host path '{source}'. Use a named volume (only {', '.join(ALLOWED_HOST_PATHS)} may come from the host)"
    # Relative to the compose directory: Dokploy keeps persistent files in ../files.
    clean = posixpath.normpath(source)
    if (clean == ".." or clean.startswith("../")) and not (clean == "../files" or clean.startswith("../files/")):
        return "bind-mounts a path outside the compose directory. Use a named volume or ../files"
    return ""


def lint_service(name: str, spec: dict) -> "list[str]":
    problems: "list[str]" = []
    if _truthy(spec.get("privileged")):
        problems.append(f"service '{name}': privileged is set; a privileged container is root on the host. Remove 'privileged:'")
    for key in ("network_mode", *HOST_NAMESPACE_KEYS):
        if str(spec.get(key) or "").strip().lower() == "host":
            fix = "Remove it; services reach each other and Traefik on dokploy-network" if key == "network_mode" else "Remove it"
            problems.append(f"service '{name}': {key}: host shares the host's namespace with the container. {fix}")
    for cap in spec.get("cap_add") or []:
        if str(cap).upper().removeprefix("CAP_") not in ALLOWED_CAPS:
            problems.append(f"service '{name}': cap_add '{cap}' is not allowed (allowed: {', '.join(ALLOWED_CAPS)}). Remove it")
    if spec.get("devices"):
        problems.append(f"service '{name}': devices: gives the container host devices. Remove 'devices:'")
    if spec.get("device_cgroup_rules"):
        problems.append(f"service '{name}': device_cgroup_rules: gives the container host devices. Remove it")
    for option in spec.get("security_opt") or []:
        flat = str(option).lower().replace("=", ":").replace(" ", "")
        if "unconfined" in flat or flat in ("label:disable", "no-new-privileges:false"):
            problems.append(f"service '{name}': security_opt '{option}' turns off confinement. Remove it")
    for volume in spec.get("volumes") or []:
        for source, target, is_bind in _sources(volume):
            if not is_bind and not any(sock in (source + target).lower() for sock in SOCKET_NAMES):
                continue
            why = _host_path_problem(source, target)
            if why:
                problems.append(f"service '{name}': volume '{source}:{target}' {why}")
    return problems


def lint(compose) -> "list[str]":
    """Every host-escape problem in a parsed compose document (``[]`` when it is fine)."""
    if not isinstance(compose, dict):
        return []
    problems: "list[str]" = []
    for name, spec in (compose.get("services") or {}).items():
        if isinstance(spec, dict):
            problems.extend(lint_service(str(name), spec))
    # A named volume can still be a bind mount of any host path through driver options.
    for name, spec in (compose.get("volumes") or {}).items():
        opts = (spec or {}).get("driver_opts") if isinstance(spec, dict) else None
        if isinstance(opts, dict):
            device = str(opts.get("device") or "")
            if "bind" in str(opts.get("o") or "") or str(opts.get("type") or "") == "none":
                if device and posixpath.normpath(device) not in ALLOWED_HOST_PATHS:
                    problems.append(f"volume '{name}': driver_opts bind-mount the host path '{device}'. Use a plain named volume")
    return problems


def main(argv: "list[str]") -> int:
    """Print the problems of a compose file and exit 1 if there are any.

    With no argument it lints the rendered deploy compose (``.build/deploy-compose.yml``)
    when the release job made one, else the compose file named in ``sealedstack.yaml``.
    """
    import os

    import yaml

    if argv:
        path = argv[0]
    elif os.path.exists(".build/deploy-compose.yml"):
        path = ".build/deploy-compose.yml"
    else:
        with open("sealedstack.yaml", encoding="utf-8") as handle:
            path = (yaml.safe_load(handle) or {}).get("compose", "docker-compose.yml")
    with open(path, encoding="utf-8") as handle:
        problems = lint(yaml.safe_load(handle) or {})
    for problem in problems:
        print(f"::error::compose lint: {problem}" if os.environ.get("GITHUB_ACTIONS") else f"compose lint: {problem}", file=sys.stderr)
    if problems:
        print(f"compose lint: {len(problems)} problem(s) in {path}; host-escape settings are refused (D-063)", file=sys.stderr)
        return 1
    print(f"compose lint: {path} has no host-escape settings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
