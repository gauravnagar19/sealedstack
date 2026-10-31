"""D-064 / DR-5: the first SSH login can be pinned to the server's host key."""
from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from sealedstack import config as config_mod, hostkey, preflight, server
from test_engine_preflight import make_ctx
from test_engine_server import FakeIO, FakeSource, make_config, fake_generated

REPO = Path(__file__).resolve().parents[2]

# Two real ed25519 host keys and their `ssh-keygen -l` SHA256 fingerprints.
KEY_A = "AAAAC3NzaC1lZDI1NTE5AAAAIH3NQw3owo2x58guE/qxa4U/wSlCVKGeyvVqe1JsgCVL"
FP_A = "SHA256:1dvnVwnkKOcZfGuJdEqf6S+PfDN1HqhUvChOheO1/DM"
KEY_B = "AAAAC3NzaC1lZDI1NTE5AAAAIEgkF3NfrBR2Hc0edbx/psnj7xKSEdnn6n20NmNwvQgU"
FP_B = "SHA256:BE1GZomy0q35sAgswMA8RIsrjsIvX3ByZjKdOHiyVn4"
IP = "203.0.113.10"


def scan(*keys):
    lines = [f"# {IP}:22 SSH-2.0-OpenSSH_9.6"]
    lines += [f"{IP} ssh-ed25519 {blob}" for blob in keys]
    return "\n".join(lines) + "\n"


# -- the fingerprint maths ------------------------------------------------------------
def test_the_fingerprint_matches_ssh_keygen_output():
    assert hostkey.fingerprint(KEY_A) == FP_A
    assert hostkey.fingerprint(KEY_B) == FP_B


def test_normalise_accepts_sha256_form_only():
    assert hostkey.normalise(FP_A) == FP_A
    assert hostkey.normalise(FP_A + "=") == FP_A  # padding is tolerated
    for bad in ("", "1dvnVwnkKOcZfGuJdEqf6S+PfDN1HqhUvChOheO1/DM", "MD5:aa:bb", "SHA256:short"):
        with pytest.raises(hostkey.HostKeyError, match="not a SHA256 fingerprint"):
            hostkey.normalise(bad)


def test_the_matching_key_becomes_the_only_known_hosts_line():
    line = hostkey.known_hosts_line(scan(KEY_B, KEY_A), IP, FP_A)
    assert line == f"{IP} ssh-ed25519 {KEY_A}\n"


def test_a_mismatch_is_refused_and_says_what_was_presented_without_the_address():
    with pytest.raises(hostkey.HostKeyError) as err:
        hostkey.known_hosts_line(scan(KEY_B), IP, FP_A, name="'server-1'")
    text = str(err.value)
    assert FP_A in text and FP_B in text and "Refusing to send the root password" in text
    assert IP not in text  # the public IP is a secret (D-049)


def test_no_key_at_all_is_a_clear_error():
    with pytest.raises(hostkey.HostKeyError, match="could not read an SSH host key"):
        hostkey.known_hosts_line("# nothing\n", IP, FP_A)


# -- server up ----------------------------------------------------------------------------
class PinIO(FakeIO):
    def __init__(self, scan_output):
        super().__init__(captures={})
        self.scan_output = scan_output
        self.known_hosts_seen = []
        self.keyscans = 0

    def capture(self, argv, env):
        self.captures.append(list(argv))
        if argv[0] == "ssh-keyscan":
            self.keyscans += 1
            out = self.scan_output[min(self.keyscans, len(self.scan_output)) - 1] if isinstance(self.scan_output, list) else self.scan_output
            return 0, out
        return 0, "tskey-auth-xyz" if "tailscalekey" in " ".join(argv) else ""

    def run(self, argv, env):
        rc = super().run(argv, env)
        if self.vars_files and self.vars_files[-1].get("bootstrap_known_hosts"):
            path = Path(self.vars_files[-1]["bootstrap_known_hosts"])
            self.known_hosts_seen.append((path.read_text(), stat.S_IMODE(path.stat().st_mode)))
        return rc


SECRETS = {"SERVER_IP_WEB_1": IP, "ROOT_PASSWORD_WEB_1": "rootpw", "TAILSCALE_OAUTH_CLIENT_ID": "id", "TAILSCALE_OAUTH_CLIENT_SECRET": "sec"}


def pinned_config(tmp_path, fingerprint):
    config = make_config(tmp_path)
    if fingerprint:
        config.servers["servers"]["web-1"]["bootstrap"] = {"host_key_fingerprint": fingerprint}
    return config


def up(tmp_path, io, fingerprint, sleeps=None):
    out = []
    server.up(pinned_config(tmp_path, fingerprint), {}, "web-1", source=FakeSource(SECRETS), io=io, generated=fake_generated(),
              out=type("O", (), {"write": lambda self, s: out.append(s), "flush": lambda self: None})(), sleep=(sleeps.append if sleeps is not None else (lambda s: None)))
    return "".join(out)


def test_match_runs_the_bootstrap_with_a_pinned_known_hosts(tmp_path):
    io = PinIO(scan(KEY_B, KEY_A))
    log = up(tmp_path, io, FP_A)
    assert io.keyscans == 1
    keyscan = next(c for c in io.captures if c[0] == "ssh-keyscan")
    assert keyscan == ["ssh-keyscan", "-T", "10", "-p", "22", "-t", "ed25519,ecdsa,rsa", IP]
    boot_vars = io.vars_files[0]
    assert boot_vars["ansible_password"] == "rootpw" and Path(boot_vars["bootstrap_known_hosts"]).name.startswith("sealedstack-known-hosts-")
    text, mode = io.known_hosts_seen[0]
    assert text == f"{IP} ssh-ed25519 {KEY_A}\n" and mode == 0o600
    assert not Path(boot_vars["bootstrap_known_hosts"]).exists()  # removed afterwards
    assert "matches the pinned fingerprint" in log and IP not in log
    assert "bootstrap_known_hosts" not in io.vars_files[1]  # the site run over the mesh is unaffected


def test_mismatch_is_refused_before_any_password_is_sent(tmp_path):
    io = PinIO(scan(KEY_B))
    with pytest.raises(hostkey.HostKeyError, match="does not match"):
        up(tmp_path, io, FP_A)
    assert not any("bootstrap.yml" in " ".join(r) for r in io.runs)  # ansible (and the password) never ran
    assert io.vars_files == []


def test_a_server_that_is_not_answering_yet_is_retried_then_refused(tmp_path):
    io = PinIO("")
    sleeps = []
    with pytest.raises(hostkey.HostKeyError, match="could not read an SSH host key"):
        up(tmp_path, io, FP_A, sleeps)
    assert io.keyscans == 4 and sleeps == [5, 5, 5]
    assert io.runs == []


def test_a_server_that_answers_on_the_second_try_is_accepted(tmp_path):
    io = PinIO(["", scan(KEY_A)])
    up(tmp_path, io, FP_A)
    assert io.keyscans == 2 and io.known_hosts_seen


def test_absent_keeps_todays_behaviour_and_says_so(tmp_path):
    io = PinIO(scan(KEY_A))
    log = up(tmp_path, io, "")
    assert io.keyscans == 0
    assert "bootstrap_known_hosts" not in io.vars_files[0]
    assert "no bootstrap.host_key_fingerprint" in log and "will not verify" in log


def test_a_malformed_fingerprint_fails_before_anything_runs(tmp_path):
    io = PinIO(scan(KEY_A))
    with pytest.raises(hostkey.HostKeyError, match="not a SHA256 fingerprint"):
        up(tmp_path, io, "not-a-fingerprint")
    assert io.keyscans == 0 and io.runs == []


# -- check ------------------------------------------------------------------------------------
def check_rows(tmp_path, fingerprint):
    stack = {"name": "x", "slots": {"compute": "any-ssh"}, "mesh": {"tailnet": "tail1234.ts.net"}}
    server_spec = {"provider": "netcup", "role": "control"}
    if fingerprint:
        server_spec["bootstrap"] = {"host_key_fingerprint": fingerprint}
    cdir = tmp_path / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    for name, doc in (("stack", stack), ("servers", {"servers": {"web-1": server_spec}}), ("apps", {"apps": {}})):
        (cdir / f"{name}.yaml").write_text(yaml.safe_dump(doc))
    config = config_mod.load_config(config_dir=cdir, code_root=REPO, env={})
    from sealedstack.secrets import EnvSource

    env = {"SERVER_IP_WEB_1": IP, "ROOT_PASSWORD_WEB_1": "pw"}
    ctx = make_ctx(tmp_path, config, env=env, secrets=EnvSource(env), tcp_probe=lambda host, port, timeout: host == IP)
    return {r.id: r for r in preflight.check_servers(ctx)}


def test_check_warns_when_the_fingerprint_is_absent_and_says_where_to_find_it(tmp_path):
    row = check_rows(tmp_path, "")["servers.web-1.host_key"]
    assert row.status == "warn"
    assert "root password" in row.detail and "host_key_fingerprint" in row.fix
    assert "Server Control Panel" in row.fix and "ssh-keygen" in row.fix


def test_check_is_green_when_it_is_pinned_and_fails_when_malformed(tmp_path):
    assert check_rows(tmp_path, FP_A)["servers.web-1.host_key"].status == "ok"
    bad = check_rows(tmp_path / "b", "SHA256:nope")["servers.web-1.host_key"]
    assert bad.status == "fail"


def test_check_says_nothing_about_servers_already_on_the_tailnet(tmp_path):
    stack = {"name": "x", "slots": {"compute": "any-ssh"}, "mesh": {"tailnet": "tail1234.ts.net"}}
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump(stack))
    (cdir / "servers.yaml").write_text(yaml.safe_dump({"servers": {"web-1": {"provider": "netcup", "role": "control"}}}))
    (cdir / "apps.yaml").write_text("apps: {}\n")
    config = config_mod.load_config(config_dir=cdir, code_root=REPO, env={})
    ctx = make_ctx(tmp_path, config, tcp_probe=lambda host, port, timeout: True)
    assert not any(r.id.endswith(".host_key") for r in preflight.check_servers(ctx))


# -- schema and playbook -----------------------------------------------------------------------
def test_the_schema_accepts_a_sha256_fingerprint_and_rejects_others():
    import jsonschema

    schema = json.loads((REPO / "schemas" / "servers.schema.json").read_text())

    def errors(fp):
        doc = {"servers": {"web-1": {"provider": "netcup", "role": "control", "os": "ubuntu-24.04", "bootstrap": {"host_key_fingerprint": fp}}}}
        return list(jsonschema.Draft202012Validator(schema).iter_errors(doc))

    assert errors(FP_A) == [] and errors(FP_A + "=") == []
    assert errors("MD5:aa:bb") and errors("SHA256:short") and errors("")


def test_the_bootstrap_play_turns_host_key_checking_on_only_with_a_pinned_file():
    play = yaml.safe_load((REPO / "ansible" / "playbooks" / "bootstrap.yml").read_text())[0]
    variables = play["vars"]
    assert variables["bootstrap_known_hosts"] == ""
    assert "bootstrap_known_hosts | length > 0" in variables["ansible_host_key_checking"]
    common = variables["ansible_ssh_common_args"]
    assert "UserKnownHostsFile=' ~ bootstrap_known_hosts" in common and "StrictHostKeyChecking=yes" in common
    text = (REPO / "ansible" / "playbooks" / "bootstrap.yml").read_text()
    # the keyed-SSH check to the admin user uses the same pinned file when there is one
    assert "'UserKnownHostsFile=' ~ bootstrap_known_hosts" in text and "accept-new" in text  # unpinned path unchanged
    assert not re.search(r"ansible_host_key_checking:\s*false", text)
