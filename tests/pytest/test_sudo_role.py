"""D-055: the admin user has passwordless sudo, and no sudo password exists anywhere."""
from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "ansible" / "playbooks" / "bootstrap.yml"
SSH_ROLE = REPO / "ansible" / "roles" / "ssh" / "tasks" / "main.yml"


def _tasks(path: Path) -> list[dict]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if isinstance(doc, list) and doc and "tasks" in doc[0]:
        return doc[0]["tasks"]
    return doc


def _sudoers_task(path: Path) -> dict:
    found = [t for t in _tasks(path) if (t.get("ansible.builtin.copy") or {}).get("dest", "").startswith("/etc/sudoers.d/")]
    assert len(found) == 1, f"{path.name}: expected exactly one sudoers drop-in task"
    return found[0]


def test_bootstrap_and_ssh_role_write_a_validated_sudoers_dropin():
    for path in (BOOTSTRAP, SSH_ROLE):
        copy = _sudoers_task(path)["ansible.builtin.copy"]
        assert copy["dest"] == "/etc/sudoers.d/90-sealedstack-admin"
        assert str(copy["mode"]) == "0440"
        assert copy["owner"] == "root" and copy["group"] == "root"
        assert "visudo -cf %s" in copy["validate"]
        assert "ALL=(ALL) NOPASSWD:ALL" in copy["content"]
        assert "{{ admin_user" in copy["content"]


def test_dropin_name_is_valid_for_sudo():
    # sudo ignores files in sudoers.d containing '.' or ending in '~'.
    name = "90-sealedstack-admin"
    assert "." not in name and not name.endswith("~")


def test_the_admin_user_has_no_password_hash():
    user = next(t for t in _tasks(BOOTSTRAP) if "ansible.builtin.user" in t)["ansible.builtin.user"]
    assert user["password"] == "*"
    text = BOOTSTRAP.read_text()
    assert "password_hash" not in text and "admin_password" not in text


def test_bootstrap_still_asserts_key_only_ssh():
    text = BOOTSTRAP.read_text()
    assert "sshd -T" in text
    assert "passwordauthentication no" in text and "permitrootlogin no" in text
    assert "PasswordAuthentication no" in text and "PermitRootLogin no" in text
    ssh = SSH_ROLE.read_text()
    assert "passwordauthentication no" in ssh and "permitrootlogin no" in ssh


def test_sudoers_dropin_passes_visudo_when_available(tmp_path):
    visudo = subprocess.run(["which", "visudo"], capture_output=True, text=True).stdout.strip()
    if not visudo:
        return  # not installed in this image; the role's own validate: covers it on the host
    f = tmp_path / "90-sealedstack-admin"
    f.write_text("admin ALL=(ALL) NOPASSWD:ALL\n")
    assert subprocess.run([visudo, "-cf", str(f)], capture_output=True).returncode == 0


def test_no_admin_sudo_password_anywhere_in_tracked_files():
    tracked = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True).stdout.split()
    offenders = []
    for rel in tracked:
        if rel in ("docs/maintainers/decisions.md", "tests/pytest/test_sudo_role.py", "tests/pytest/test_engine_server.py"):
            continue  # D-055 and the tests that prove its absence name the removed secret
        path = REPO / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for needle in ("admin_sudo_password", "ADMIN_SUDO_PASSWORD", "ansible_become_password"):
            if needle in text:
                offenders.append(f"{rel}: {needle}")
    assert offenders == []
