"""DR-2 + DR-3 (D-062): one lock for CI writes and break-glass, with an expiry."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).parent))

from sealedstack import config as cfg, infraci, lock, statusissue  # noqa: E402

from test_infra_ci import SHA, SLUG, ev, make_config, make_forge, merged_push, runner_for  # noqa: E402

T0 = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def make_lock(backend, owner, clock, **kw):
    warnings = []
    state = lock.StateLock(backend, owner=owner, key=lock.lock_key("my-infra"), clock=clock, warn=warnings.append, **kw)
    state.warnings = warnings
    return state


# -- the lock itself -----------------------------------------------------------------
def test_the_lock_body_has_id_owner_created_expires_and_run_url():
    clock = Clock()
    backend = lock.MemoryBackend()
    info = make_lock(backend, "ci:you", clock, run_url="https://github.com/you/my-infra/actions/runs/9").acquire()
    stored = json.loads(backend.store["sealedstack/my-infra/state.lock"])
    assert set(stored) == {"id", "owner", "created", "expires", "run_url"}
    assert stored == info
    assert stored["created"] == "2026-10-30T12:00:00Z" and stored["expires"] == "2026-10-30T14:00:00Z"  # 2 h
    assert stored["owner"] == "ci:you" and stored["run_url"].endswith("/actions/runs/9")


def test_a_live_lock_is_refused_with_holder_age_and_the_force_unlock_hint():
    clock, backend = Clock(), lock.MemoryBackend()
    held = make_lock(backend, "break-glass:alice", clock, run_url="https://x/run/1").acquire()
    clock.advance(minutes=25)
    with pytest.raises(lock.LockHeld) as err:
        make_lock(backend, "ci:bob", clock).acquire()
    text = str(err.value)
    assert "break-glass:alice" in text and held["id"] in text and "25 min" in text
    assert f"--force-unlock {held['id']}" in text and "https://x/run/1" in text


def test_an_expired_lock_is_taken_over_with_a_warning_naming_the_previous_holder():
    clock, backend = Clock(), lock.MemoryBackend()
    old = make_lock(backend, "ci:runner-killed", clock).acquire()
    clock.advance(hours=2, minutes=1)
    newcomer = make_lock(backend, "ci:next", clock)
    info = newcomer.acquire()
    assert info["id"] != old["id"] and newcomer.holder()["owner"] == "ci:next"
    assert len(newcomer.warnings) == 1
    assert "expired" in newcomer.warnings[0] and "ci:runner-killed" in newcomer.warnings[0] and old["id"] in newcomer.warnings[0]


def test_a_lock_just_before_expiry_is_not_taken_over():
    clock, backend = Clock(), lock.MemoryBackend()
    make_lock(backend, "ci:a", clock).acquire()
    clock.advance(hours=1, minutes=59)
    with pytest.raises(lock.LockHeld):
        make_lock(backend, "ci:b", clock).acquire()


def test_release_removes_only_the_callers_own_lock():
    clock, backend = Clock(), lock.MemoryBackend()
    a = make_lock(backend, "ci:a", clock)
    mine = a.acquire()
    clock.advance(hours=3)
    b = make_lock(backend, "ci:b", clock)
    b.acquire()  # took over a's expired lock
    assert a.release(mine) is False  # a's late release must not free b's lock
    assert b.holder()["owner"] == "ci:b"


def test_an_unreadable_lock_body_counts_as_held_and_can_be_force_unlocked():
    clock, backend = Clock(), lock.MemoryBackend({"sealedstack/my-infra/state.lock": b"not json"})
    with pytest.raises(lock.LockHeld) as err:
        make_lock(backend, "ci:a", clock).acquire()
    assert "--force-unlock unknown" in str(err.value)
    assert make_lock(backend, "ci:a", clock).force_unlock("unknown") is True


def test_stale_report_only_after_an_hour():
    clock, backend = Clock(), lock.MemoryBackend()
    state = make_lock(backend, "ci:a", clock)
    assert state.stale_report() == ""
    state.acquire()
    clock.advance(minutes=59)
    assert state.stale_report() == ""
    clock.advance(minutes=2)
    report = state.stale_report()
    assert "61 min" in report and "ci:a" in report and "--force-unlock" in report


# -- CI exec takes the lock for writes ----------------------------------------------
def apply_event(f):
    merged_push(f)
    return ev("workflow_dispatch", actor="you", command="apply")


def test_ci_writes_hold_the_lock_during_the_run_and_release_it_after(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    state = make_lock(backend, "ci:you", clock)
    event = apply_event(f)
    seen_during = []

    def run(argv):
        seen_during.append(state.holder())
        return 0, "ok"

    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, run, lock_fn=lambda: state) == 0
    assert seen_during[0]["owner"] == "ci:you"  # held while the command ran
    assert state.holder() is None  # released afterwards


def test_ci_refuses_while_break_glass_holds_the_lock(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    held = make_lock(backend, "break-glass:alice", clock).acquire()
    event = apply_event(f)
    calls = []
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(calls=calls),
                           lock_fn=lambda: make_lock(backend, "ci:you", clock)) == 1
    assert calls == []  # nothing ran
    body = f.comments[(SLUG, 7)][0].body
    assert "break-glass:alice" in body and f"--force-unlock {held['id']}" in body
    assert json.loads(backend.store["sealedstack/my-infra/state.lock"])["id"] == held["id"]  # untouched


def test_break_glass_refuses_while_ci_holds_the_lock(tmp_path):
    clock, backend = Clock(), lock.MemoryBackend()
    make_lock(backend, "ci:you", clock, run_url="https://github.com/you/my-infra/actions/runs/5").acquire()

    def clean_git(args):  # a clean tree, HEAD == origin/main
        return (0, "abc", "") if args[:1] == ["rev-parse"] else (0, "", "")

    runtime = cfg.Runtime(REPO, {}, clean_git)
    with pytest.raises(cfg.BreakGlassError) as err:
        runtime.guard_write("apply", "my-infra", break_glass=True, confirm_fn=lambda _: "my-infra", lock=make_lock(backend, "break-glass:alice", clock))
    assert "ci:you" in str(err.value) and "--force-unlock" in str(err.value) and "actions/runs/5" in str(err.value)


def test_the_lock_is_released_when_the_command_fails(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    state = make_lock(backend, "ci:you", clock)
    event = apply_event(f)
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for("Error", rc=1), lock_fn=lambda: state) == 1
    assert state.holder() is None


def test_the_lock_is_released_when_the_runner_raises(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    state = make_lock(backend, "ci:you", clock)
    event = apply_event(f)

    def boom(argv):
        raise RuntimeError("runner crashed")

    with pytest.raises(RuntimeError):
        infraci.execute(infraci.resolve(event, f, c), event, f, c, boom, lock_fn=lambda: state)
    assert state.holder() is None


def test_reads_never_take_the_lock(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    built = []
    for command in ("plan", "check", "drift"):
        event = ev("workflow_dispatch", actor="you", command=command)
        res = infraci.resolve(event, f, c)
        assert not res.write
        assert infraci.execute(res, event, f, c, runner_for(), lock_fn=lambda: built.append(1)) == 0
    assert built == []  # the factory was never even called


def test_an_expired_lock_is_taken_over_by_a_ci_write_and_released(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    make_lock(backend, "ci:killed", clock).acquire()
    clock.advance(hours=3)
    state = make_lock(backend, "ci:you", clock)
    event = apply_event(f)
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(), lock_fn=lambda: state) == 0
    assert state.warnings and "ci:killed" in state.warnings[0]
    assert state.holder() is None


def test_a_missing_lock_is_a_warning_not_a_failure(tmp_path, capsys):
    f, c = make_forge(), make_config(tmp_path)
    event = apply_event(f)
    assert infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(), lock_fn=lambda: None) == 0
    assert "no remote state lock" in capsys.readouterr().err


# -- the Status issue ----------------------------------------------------------------
def status_run(f, c, lock_obj):
    event = ev("schedule", actor="github-actions")
    res = infraci.resolve(event, f, c)
    results = [type("R", (), {"id": "x", "status": "ok", "detail": "", "fix": "", "area": "a"})()]
    return infraci.execute(res, event, f, c, runner_for(), status_fn=lambda: (c, "0.6.0", results), lock_fn=lambda: lock_obj)


def test_the_status_issue_reports_a_lock_held_for_over_an_hour(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    clock, backend = Clock(), lock.MemoryBackend()
    holder = make_lock(backend, "ci:you", clock)
    holder.acquire()
    clock.advance(minutes=30)
    assert status_run(f, c, make_lock(backend, "ci:status", clock)) == 0
    number = statusissue.issue_number(f, SLUG)
    assert f.comments.get((SLUG, number), []) == []  # 30 min: fine
    clock.advance(minutes=45)
    status_run(f, c, make_lock(backend, "ci:status", clock))
    status_run(f, c, make_lock(backend, "ci:status", clock))  # hourly runs edit one comment, not many
    comments = f.comments[(SLUG, number)]
    assert len(comments) == 1 and "state lock" in comments[0].body and "ci:you" in comments[0].body and "--force-unlock" in comments[0].body


def test_status_does_not_fail_when_the_lock_cannot_be_read(tmp_path):
    f, c = make_forge(), make_config(tmp_path)

    class Broken:
        def stale_report(self):
            raise RuntimeError("R2 unreachable")

    assert status_run(f, c, Broken()) == 0


# -- end to end over HTTP against the strict fake S3 --------------------------------
from test_s3_addressing import ENV, _config, fake_r2  # noqa: E402,F401  (fixture reused)


def test_ci_and_break_glass_exclude_each_other_through_the_real_client(tmp_path, fake_r2):
    from sealedstack import cli

    config = _config(tmp_path)
    env = {**ENV, "GITHUB_ACTOR": "you", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "you/my-infra", "GITHUB_RUN_ID": "77"}
    ci_lock = infraci._default_lock_fn(config, env)()
    held = ci_lock.acquire()
    stored = json.loads(fake_r2.objects("sealedstack-state")["sealedstack/my-infra/state.lock"])
    assert stored["owner"] == "ci:you" and stored["run_url"] == "https://github.com/you/my-infra/actions/runs/77"
    # break-glass wants the same object: refused (HTTP 412 from the server), naming the CI holder
    with pytest.raises(lock.LockHeld) as err:
        cli._state_lock(config, {**ENV, "USER": "alice"}, kind="break-glass").acquire()
    assert "ci:you" in str(err.value) and held["id"] in str(err.value)
    assert [r["status"] for r in fake_r2.requests if r["method"] == "PUT"] == [200, 412]
    assert ci_lock.release(held) is True
    other = cli._state_lock(config, {**ENV, "USER": "alice"}, kind="break-glass").acquire()
    assert json.loads(fake_r2.objects("sealedstack-state")["sealedstack/my-infra/state.lock"])["owner"] == "break-glass:alice"
    assert other["id"]


def test_the_run_container_gets_the_run_identity_for_the_lock_body():
    import yaml

    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))
    step = next(s for s in workflow["jobs"]["run"]["steps"] if str(s.get("name", "")).startswith("sealedstack "))
    assert "-e GITHUB_SERVER_URL" in step["run"] and "-e GITHUB_RUN_ID" in step["run"]
    assert step["run"].rstrip().endswith("ci exec")  # writes take the lock inside `ci exec`, behind the approval gate


# -- before the first `up` there is no state bucket, so there is nothing to lock (live run, 2026-11-01) ----
@pytest.fixture
def no_bucket_r2(monkeypatch):
    from fake_s3 import ACCESS_KEY, SECRET_KEY, FakeS3
    from sealedstack import cli, r2

    with FakeS3(buckets=()) as server:
        monkeypatch.setattr(cli, "r2_endpoint", lambda account: server.endpoint)
        monkeypatch.setattr(r2, "resolve", lambda source, config, env, http=None, required=True: r2.R2Credentials(ACCESS_KEY, SECRET_KEY, True))
        yield server


def test_the_s3_client_names_a_missing_bucket_as_such(no_bucket_r2, tmp_path):
    from sealedstack import cli, s3

    client = cli._r2_client(_config(tmp_path), ENV)
    with pytest.raises(s3.S3BucketMissing):
        client.put_if_absent("k", b"{}")


def test_a_ci_write_runs_without_a_lock_when_the_state_bucket_does_not_exist_yet(tmp_path, no_bucket_r2, capsys):
    f, c = make_forge(), make_config(tmp_path)
    event = apply_event(f)
    ran = []
    rc = infraci.execute(
        infraci.resolve(event, f, c), event, f, c,
        lambda argv: (ran.append(argv), (0, "ok"))[1],
        lock_fn=infraci._default_lock_fn(_config(tmp_path), ENV),
    )
    assert rc == 0 and ran
    err = capsys.readouterr().err
    assert "no state lock yet" in err and "first `up` creates the state bucket" in err
    assert "Traceback" not in err


def test_other_storage_errors_still_fail_the_run(tmp_path):
    class Broken:
        def acquire(self):
            from sealedstack.s3 import S3Error

            raise S3Error("conditional put k failed (HTTP 403)")

    f, c = make_forge(), make_config(tmp_path)
    event = apply_event(f)
    with pytest.raises(Exception, match="HTTP 403"):
        infraci.execute(infraci.resolve(event, f, c), event, f, c, runner_for(), lock_fn=lambda: Broken())


def test_break_glass_falls_back_to_the_local_file_lock_without_a_bucket(tmp_path, no_bucket_r2):
    from sealedstack import cli

    state = cli._state_lock(_config(tmp_path), {**ENV, "USER": "alice"}, kind="break-glass")
    with pytest.raises(lock.LockUnavailable):
        state.acquire()
