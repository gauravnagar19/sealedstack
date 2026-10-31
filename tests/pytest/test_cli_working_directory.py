"""The CLI must run commands in the caller's directory (the infra repo), not the engine's.

Bug: ``cli.default_runner`` forced ``cwd=code_root``. In the engine image that is
``/opt/sealedstack``, so ``sealedstack setup`` looked for ``sealedstack.version`` there
instead of in the mounted infra repo (``/infra``), and always failed. ``debug onboard``
("the app checkout, default: current directory") had the same problem, and ``ci exec``
passed the wrong directory on to the commands it runs. The Setup journey tests called
``setupjob.run(..., root=tmp_path)`` directly, so no test went through the CLI and a real
subprocess; these do.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import cli  # noqa: E402


def clean_git():
    def runner(args):
        if args == ["status", "--porcelain"]:
            return 0, "", ""
        return 0, "abc", ""

    return runner


def make_infra_repo(tmp_path: Path) -> Path:
    """What a user has after 'step 2: add two files'."""
    infra = tmp_path / "infra"
    (infra / ".github" / "workflows").mkdir(parents=True)
    (infra / "sealedstack.version").write_text("0.6.0\n", encoding="utf-8")
    (infra / ".github" / "workflows" / "sealedstack.yml").write_text(
        "jobs:\n  s:\n    uses: you/sealedstack/.github/workflows/infra.yml@v0.6.0\n", encoding="utf-8"
    )
    return infra


def use_directory(monkeypatch, directory: Path) -> None:
    """Like ``docker run -w /infra``: the process starts in the repo and the engine is importable."""
    monkeypatch.chdir(directory)
    monkeypatch.setenv("PYTHONPATH", str(REPO / "engine"))  # the image sets this; a checkout needs it
    monkeypatch.setenv("SEALEDSTACK_CONFIG", str(directory / "config"))


def test_the_default_runner_does_not_move_the_command_to_the_code_root(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.default_runner(["python3", "-c", "pass"], Path("/opt/sealedstack")) == 0
    assert seen["kwargs"].get("cwd") is None  # inherited: the caller's directory


def test_setup_through_the_cli_finds_the_infra_repo_and_writes_there(tmp_path, monkeypatch):
    infra = make_infra_repo(tmp_path)
    use_directory(monkeypatch, infra)
    rc = cli.run(
        ["setup", "--local", "--domain", "example.com", "--email", "you@example.com", "--server", "server-1",
         "--secrets", "bitwarden", "--owner", "you", "--name", "my-infra"],
        code_root=REPO,
        env={},
        git=clean_git(),
    )
    assert rc == 0
    for written in ("config/stack.yaml", "config/servers.yaml", "config/apps.yaml", ".devcontainer/devcontainer.json"):
        assert (infra / written).is_file(), written
    # and nothing was written into the engine's own tree
    assert not (REPO / "config" / "stack.yaml").exists()
    assert not (REPO / ".devcontainer").exists()


def test_setup_still_says_what_is_missing_when_the_repo_really_lacks_the_version_file(tmp_path, monkeypatch, capfd):
    empty = tmp_path / "empty"
    empty.mkdir()
    use_directory(monkeypatch, empty)
    rc = cli.run(
        ["setup", "--local", "--domain", "example.com", "--email", "you@example.com", "--server", "server-1"],
        code_root=REPO, env={}, git=clean_git(),
    )
    assert rc != 0
    assert "sealedstack.version is missing" in capfd.readouterr().err
    assert not (empty / "config").exists()


def test_debug_onboard_works_on_the_checkout_it_is_run_in(tmp_path, monkeypatch, capfd):
    app = tmp_path / "app"
    app.mkdir()
    (app / "docker-compose.yml").write_text(
        "services:\n  web:\n    build: .\n    ports:\n      - '3000:3000'\n", encoding="utf-8"
    )
    use_directory(monkeypatch, app)
    rc = cli.run(
        ["debug", "onboard", "--name", "example-app", "--repo", "you/example-app"],
        code_root=REPO, env={}, git=clean_git(),
    )
    out = capfd.readouterr()
    assert rc == 0, out.err
    assert "docker-compose.yml" in out.out + out.err  # it looked at THIS checkout, not the engine's tree
