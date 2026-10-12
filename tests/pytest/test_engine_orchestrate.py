"""Tests for the Python orchestration (plan/apply/up) and native status."""
from __future__ import annotations

import json
from pathlib import Path

from sealedstack import orchestrate


def _fake_runner(record):
    def runner(argv, code_root):
        record.append(" ".join(list(argv)))
        return 0
    return runner


def test_plan_phases_order():
    record = []
    rc = orchestrate.run_phases("plan", code_root=Path("/repo"), runner=_fake_runner(record))
    assert rc == 0
    assert any("render.py" in x for x in record)
    assert any("mesh-policy.py" in x for x in record)
    assert record[-1].endswith("sealedstack.platform plan")
    # plan never writes
    assert not any("sealedstack.edge apply" in x for x in record)
    assert not any("sealedstack.platform apply" in x for x in record)


def test_apply_phases_include_writes_and_publish():
    record = []
    rc = orchestrate.run_phases("apply", code_root=Path("/repo"), runner=_fake_runner(record))
    assert rc == 0
    joined = " ".join(record)
    assert "sealedstack.edge apply" in joined
    assert "sealedstack.platform apply" in joined
    assert "sealedstack.postapply" in joined
    assert "sealedstack.publishsettings" in joined


def test_up_includes_state_and_server_bootstrap():
    record = []
    rc = orchestrate.run_phases(
        "up",
        code_root=Path("/repo"),
        runner=_fake_runner(record),
        servers=["web-1"],
        server_ips={"web-1": "203.0.113.10"},
    )
    assert rc == 0
    joined = " ".join(record)
    assert "sealedstack.stateinit" in joined
    assert "sealedstack.server up web-1" in joined
    idx_edge = next(i for i, x in enumerate(record) if "sealedstack.edge apply" in x)
    idx_server = next(i for i, x in enumerate(record) if "sealedstack.server up" in x)
    idx_platform = next(i for i, x in enumerate(record) if "sealedstack.platform apply" in x)
    assert idx_edge < idx_server < idx_platform


def test_failure_stops_and_is_rerunnable():
    record = []

    def failing_runner(argv, code_root):
        record.append(" ".join(list(argv)))
        if argv and "sealedstack.edge" in argv:
            return 7
        return 0

    rc = orchestrate.run_phases("apply", code_root=Path("/repo"), runner=failing_runner)
    assert rc == 7
    assert not any("sealedstack.platform" in x for x in record)

    record2 = []
    rc2 = orchestrate.run_phases("apply", code_root=Path("/repo"), runner=_fake_runner(record2))
    assert rc2 == 0


def test_json_output_records_phases(capsys):
    record = []
    rc = orchestrate.run_phases("plan", code_root=Path("/repo"), runner=_fake_runner(record), json_output=True)
    assert rc == 0
    out = capsys.readouterr().out.strip().splitlines()
    payload = json.loads(out[-1])
    assert payload["command"] == "plan"
    assert payload["ok"] is True
    assert len(payload["phases"]) >= 4


def test_up_resumes_after_midway_failure():
    """`up` is safe to re-run: a failure mid-way is resumed, not restarted."""
    calls = []

    def runner_that_fails_once(argv, code_root):
        joined = " ".join(list(argv))
        calls.append(joined)
        # fail the first time we reach platform-apply
        if "sealedstack.platform apply" in joined and calls.count(joined) == 1:
            return 3
        return 0

    rc = orchestrate.run_phases(
        "up", code_root=Path("/repo"), runner=runner_that_fails_once, servers=["web-1"]
    )
    assert rc == 3
    assert any("sealedstack.platform apply" in c for c in calls)
    # nothing after the failing phase ran
    assert not any("sealedstack.postapply" in c for c in calls)


def test_up_completes_on_rerun():
    record = []
    rc = orchestrate.run_phases("up", code_root=Path("/repo"), runner=_fake_runner(record), servers=["web-1"])
    assert rc == 0
    joined = " ".join(record)
    assert "sealedstack.stateinit" in joined and "sealedstack.platform apply" in joined and "sealedstack.postapply" in joined
