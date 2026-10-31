"""DR-12: the IPv6 DOCKER-USER rule uses the IPv6 default-route interface, found separately."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
ROLE = REPO / "ansible" / "roles" / "firewall"
ANSIBLE = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(ANSIBLE is None, reason="ansible-playbook is only in the toolbox image")

V4 = "default via 203.0.113.1 dev eth0 proto dhcp src 203.0.113.10 metric 100\n"
V6 = "default via fe80::1 dev {dev} proto ra metric 1024 expires 1700sec pref medium\n"


def resolve(tmp_path, route4, route6=None):
    """Run the role's interfaces.yml with canned `ip` output; return the facts it computes."""
    play = {
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [
            {"ansible.builtin.set_fact": {"firewall_default_route": {"stdout": route4}}},
            *([{"ansible.builtin.set_fact": {"firewall_default_route6": {"stdout": route6}}}] if route6 is not None else []),
            {"ansible.builtin.include_tasks": str(ROLE / "tasks" / "interfaces.yml")},
            {"ansible.builtin.debug": {"msg": {"v4": "{{ firewall_wan_if }}", "v6": "{{ firewall_wan6_if }}", "all": "{{ firewall_wan6_interfaces }}"}}},
        ],
    }
    (tmp_path / "p.yml").write_text(yaml.safe_dump([play]))
    done = subprocess.run([ANSIBLE, str(tmp_path / "p.yml"), "-i", "localhost,", "-e", "ansible_python_interpreter=auto_silent"],
                          capture_output=True, text=True, cwd=tmp_path, env={"ANSIBLE_STDOUT_CALLBACK": "json", "ANSIBLE_NOCOLOR": "1", "PATH": "/usr/bin:/bin:/opt/venv/bin"})
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(done.stdout)
    return result["plays"][0]["tasks"][-1]["hosts"]["localhost"]["msg"]


@needs_ansible
def test_ipv6_default_route_on_another_interface_is_found_separately(tmp_path):
    facts = resolve(tmp_path, V4, V6.format(dev="tun6"))
    assert facts["v4"] == "eth0" and facts["v6"] == "tun6"
    assert facts["all"] == ["tun6", "eth0"]  # the v6 interface first; the v4 one is kept as well


@needs_ansible
def test_the_same_interface_for_both_is_listed_once(tmp_path):
    facts = resolve(tmp_path, V4, V6.format(dev="eth0"))
    assert facts["v6"] == "eth0" and facts["all"] == ["eth0"]


@needs_ansible
def test_no_ipv6_default_route_falls_back_to_the_ipv4_interface(tmp_path):
    for route6 in ("", None):
        facts = resolve(tmp_path, V4, route6)
        assert facts["v6"] == "eth0" and facts["all"] == ["eth0"]


@needs_ansible
def test_no_ipv4_default_route_falls_back_to_eth0_and_still_finds_v6(tmp_path):
    facts = resolve(tmp_path, "", V6.format(dev="ens3"))
    assert facts["v4"] == "eth0" and facts["v6"] == "ens3" and facts["all"] == ["ens3", "eth0"]


@needs_ansible
def test_the_first_of_several_default_routes_wins(tmp_path):
    two = V4 + "default via 198.51.100.1 dev wlan0 metric 600\n"
    assert resolve(tmp_path, two)["v4"] == "eth0"


def render(interfaces):
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(trim_blocks=True, lstrip_blocks=False)  # Ansible's template lookup defaults to trim_blocks
    template = env.from_string((ROLE / "templates" / "docker-user-block.j2").read_text(encoding="utf-8"))
    return template.render(mesh_interface="tailscale0", interfaces=interfaces)


def test_the_block_drops_on_every_listed_interface_and_keeps_the_order():
    text = render(["tun6", "eth0"])
    lines = text.splitlines()
    assert lines[0] == "*filter" and lines[-1] == "COMMIT"
    assert lines.index("-A DOCKER-USER -i tailscale0 -j RETURN") < lines.index("-A DOCKER-USER -i tun6 -j DROP") < lines.index("-A DOCKER-USER -i eth0 -j DROP")
    assert lines[-2] == "-A DOCKER-USER -j RETURN"
    assert text.count("-j DROP") == 2


def test_the_ipv4_block_names_only_the_ipv4_interface():
    assert render(["eth0"]).count("-j DROP") == 1


def test_the_role_uses_the_template_for_both_families_with_the_right_variables():
    tasks = yaml.safe_load((ROLE / "tasks" / "main.yml").read_text(encoding="utf-8"))
    by_name = {t["name"]: t for t in tasks}
    v4 = by_name["Close the Docker/UFW bypass (IPv4)"]
    v6 = by_name["Close the Docker/UFW bypass (IPv6)"]
    assert v4["vars"]["interfaces"] == ["{{ firewall_wan_if }}"]
    assert v6["vars"]["interfaces"] == "{{ firewall_wan6_interfaces }}"
    for task in (v4, v6):
        assert "docker-user-block.j2" in task["ansible.builtin.blockinfile"]["block"]
    detect = by_name["Detect the IPv6 default route"]
    assert detect["ansible.builtin.command"] == "ip -6 route show default" and detect["failed_when"] is False
