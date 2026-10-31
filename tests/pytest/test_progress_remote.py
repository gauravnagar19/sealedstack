"""Task 8: `up` progress is kept in the R2 state bucket, so a fresh CI runner resumes."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO / "engine") not in sys.path:
    sys.path.insert(0, str(REPO / "engine"))

from sealedstack import cli, config as cfg, orchestrate, progress, s3  # noqa: E402


class FakeS3:
    def __init__(self):
        self.objects = {}

    def get(self, key):
        return self.objects.get(key)

    def put(self, key, body):
        self.objects[key] = body

    def delete(self, key):
        self.objects.pop(key, None)


def make_runner(fail_on=None, calls=None):
    def runner(argv, code_root):
        name = next((p for p in argv if p.startswith("sealedstack.")), "") or Path(argv[1]).name
        if calls is not None:
            calls.append(name)
        return 1 if fail_on and fail_on in " ".join(argv) else 0

    return runner


def run_up(tmp_path, workdir, remote, *, fail_on=None, commit="a" * 40, calls=None):
    store = progress.Progress([progress.S3Store(remote, "sealedstack/my-infra/progress-up.json"),
                               progress.FileStore(Path(workdir) / "phases-up.json")], commit=commit)
    return orchestrate.run_phases("up", code_root=REPO, runner=make_runner(fail_on, calls), servers=["server-1"],
                                  progress=store, stream=io.StringIO())


def test_a_fresh_workdir_resumes_from_the_remote_record(tmp_path):
    remote = FakeS3()
    first_calls = []
    assert run_up(tmp_path, tmp_path / "run1", remote, fail_on="sealedstack.edge", calls=first_calls) == 1
    record = json.loads(remote.objects["sealedstack/my-infra/progress-up.json"])
    assert "state-init" in record["done"] and "edge-apply" not in record["done"] and record["commit"] == "a" * 40

    second_calls = []  # a new runner: an empty workdir, only the remote record exists
    assert run_up(tmp_path, tmp_path / "run2-fresh", remote, calls=second_calls) == 0
    assert not any("stateinit" in c for c in second_calls), "finished phases must be skipped"
    assert any("edge" in c for c in second_calls) and any("platform" in c for c in second_calls)
    assert "sealedstack/my-infra/progress-up.json" not in remote.objects, "cleared after a complete run"


def test_a_record_for_another_commit_is_ignored(tmp_path):
    remote = FakeS3()
    run_up(tmp_path, tmp_path / "a", remote, fail_on="sealedstack.edge", commit="a" * 40)
    calls = []
    assert run_up(tmp_path, tmp_path / "b", remote, commit="b" * 40, calls=calls) == 0
    assert any("stateinit" in c for c in calls), "config changed: every phase runs again"


def test_an_unreadable_remote_never_blocks_a_run(tmp_path):
    class Broken(FakeS3):
        def get(self, key):
            raise s3.S3Error("down")

        def put(self, key, body):
            raise s3.S3Error("down")

    assert run_up(tmp_path, tmp_path / "x", Broken()) == 0


def test_the_cli_builds_the_record_next_to_the_state(tmp_path):
    config = cfg.Config(REPO, tmp_path, {"name": "my-infra"}, {}, {})
    remote = FakeS3()
    store = cli._progress(config, {"GITHUB_SHA": "c" * 40}, "up", str(tmp_path / "w"), client=remote)
    store.save({"state-init"})
    assert json.loads(remote.objects["sealedstack/my-infra/progress-up.json"]) == {"commit": "c" * 40, "done": ["state-init"]}
    assert (tmp_path / "w" / "phases-up.json").exists()


def test_the_record_holds_phase_names_only(tmp_path):
    remote = FakeS3()
    run_up(tmp_path, tmp_path / "r", remote, fail_on="sealedstack.edge")
    text = remote.objects["sealedstack/my-infra/progress-up.json"].decode()
    assert set(json.loads(text)) == {"commit", "done"}


def test_s3_client_can_put_an_object_unconditionally():
    seen = []

    def sender(method, url, headers, body):
        seen.append((method, url, dict(headers)))
        from sealedstack.http import Response

        return Response(200, {}, b"", url)

    client = s3.S3Client("https://acct.r2.cloudflarestorage.com", "sealedstack-state", "id", "secret", sender=sender)
    client.put("sealedstack/x/progress-up.json", b"{}")
    assert seen[0][0] == "PUT" and "If-None-Match" not in seen[0][2]
