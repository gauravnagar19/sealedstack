"""DR-8: root never pipes a live installer script into a shell."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
ANSIBLE = REPO / "ansible"
PIPE_TO_SHELL = re.compile(r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba|da|z)?sh\b", re.I)
PROCESS_SUBST = re.compile(r"(sh|bash)\s+<\(\s*(curl|wget)", re.I)


def text_files():
    for path in sorted(ANSIBLE.rglob("*")):
        if path.is_file() and path.suffix in (".yml", ".yaml", ".j2", ".sh", ".py"):
            yield path


def test_no_curl_or_wget_piped_into_a_shell_anywhere_in_ansible():
    offenders = []
    for path in text_files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # comments may quote what is forbidden
            if PIPE_TO_SHELL.search(line) or PROCESS_SUBST.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def tasks(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def flatten(items):
    for item in items or []:
        yield item
        for key in ("block", "rescue", "always"):
            yield from flatten(item.get(key))


# -- Tailscale: signed apt repository, key fingerprint pinned --------------------------
def test_tailscale_installs_from_its_signed_apt_repository():
    play = yaml.safe_load((ANSIBLE / "playbooks" / "bootstrap.yml").read_text(encoding="utf-8"))[0]
    names = [t.get("name", "") for t in flatten(play["tasks"])]
    assert "Install Tailscale" not in names
    include = next(t for t in flatten(play["tasks"]) if "ansible.builtin.include_role" in t)
    assert include["ansible.builtin.include_role"] == {"name": "mesh_tailscale", "tasks_from": "install"}


def test_the_tailscale_install_verifies_the_pinned_key_before_trusting_it():
    defaults = yaml.safe_load((ANSIBLE / "roles" / "mesh_tailscale" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9A-F]{40}", defaults["mesh_tailscale_apt_signer_fingerprint"])
    items = list(flatten(tasks(ANSIBLE / "roles" / "mesh_tailscale" / "tasks" / "install.yml")))
    names = [t.get("name", "") for t in items]
    download = next(i for i, n in enumerate(names) if "Download the Tailscale repository signing key" in n)
    verify = next(i for i, n in enumerate(names) if "Assert the signing key is the pinned one" in n)
    keyring = next(i for i, n in enumerate(names) if "Install the verified key" in n)
    source = next(i for i, n in enumerate(names) if "Add the Tailscale apt source" in n)
    package = next(i for i, n in enumerate(names) if n == "Install Tailscale")
    assert download < verify < keyring < source < package
    assert "ansible.builtin.apt" in items[package] and items[package]["ansible.builtin.apt"]["name"] == "tailscale"
    assert "mesh_tailscale_apt_signer_fingerprint" in str(items[verify])
    text = (ANSIBLE / "roles" / "mesh_tailscale" / "tasks" / "install.yml").read_text(encoding="utf-8")
    assert "signed-by={{ mesh_tailscale_apt_keyring }}" in text and defaults["mesh_tailscale_apt_keyring"] == "/usr/share/keyrings/tailscale-archive-keyring.gpg"
    assert defaults["mesh_tailscale_apt_base_url"] == "https://pkgs.tailscale.com/stable"


# -- Dokploy: pinned installer + SHA-256 ------------------------------------------------------
def test_the_dokploy_installer_is_pinned_and_checksummed():
    defaults = yaml.safe_load((ANSIBLE / "roles" / "platform_dokploy" / "defaults" / "main.yml").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[0-9a-f]{40}", defaults["dokploy_installer_commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", defaults["dokploy_installer_sha256"])
    url = defaults["dokploy_install_url"]
    assert "dokploy.com" not in url and "{{ dokploy_installer_commit }}" in url and url.startswith("https://raw.githubusercontent.com/")
    items = list(flatten(tasks(ANSIBLE / "roles" / "platform_dokploy" / "tasks" / "main.yml")))
    fetch = next(t for t in items if "ansible.builtin.get_url" in t)
    args = fetch["ansible.builtin.get_url"]
    assert args["url"] == "{{ dokploy_install_url }}" and args["checksum"] == "sha256:{{ dokploy_installer_sha256 }}"
    run = next(t for t in items if t.get("name") == "Install Dokploy")
    assert "curl" not in str(run) and "DOKPLOY_VERSION" in str(run["environment"])
    names = [t.get("name") for t in items]
    assert names.index("Download the pinned Dokploy installer") < names.index("Install Dokploy")


def test_the_pins_are_documented_for_maintainers():
    defaults = (ANSIBLE / "roles" / "platform_dokploy" / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "dokploy_installer_commit" in defaults and "sha256sum" in defaults and "bump" in defaults.lower()
    ts = (ANSIBLE / "roles" / "mesh_tailscale" / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "gpg --show-keys" in ts


def test_renovate_bumps_the_dokploy_version():
    renovate = (REPO / "renovate.json").read_text(encoding="utf-8")
    assert "dokploy_version" in renovate and "Dokploy/dokploy" in renovate.replace("dokploy/dokploy", "Dokploy/dokploy")


def test_coolify_requires_a_checksum_too():
    defaults = (ANSIBLE / "roles" / "platform_coolify" / "defaults" / "main.yml").read_text(encoding="utf-8")
    assert "platform_coolify_install_sha256" in defaults


def test_the_detector_itself_catches_the_forbidden_shapes():
    assert PIPE_TO_SHELL.search("curl -fsSL https://example.com/install.sh | sh")
    assert PIPE_TO_SHELL.search("    wget -qO- https://example.com/i.sh | sudo bash")
    assert PROCESS_SUBST.search("bash <(curl -sSL https://example.com/i.sh)")
    assert not PIPE_TO_SHELL.search("curl -fsSL https://example.com/file -o /tmp/file")
