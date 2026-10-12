"""C2: the break-glass audit is posted to the Status issue via the forge."""
from __future__ import annotations

from pathlib import Path

from sealedstack import config as cfg, forge, statusissue


def _git(args):
    if list(args) == ["status", "--porcelain"]:
        return 0, "", ""
    return 0, "abc", ""


def test_break_glass_posts_audit_to_the_status_issue():
    f = forge.InMemoryForge()
    number = statusissue.publish(f, "me/infra", statusissue.render(name="my-infra", version="0.5.0"))

    runtime = cfg.Runtime.detect(Path("/repo"), {"USER": "operator"}, git=_git)
    posted = {}
    runtime.guard_write(
        "apply",
        "my-infra",
        break_glass=True,
        confirm_fn=lambda prompt: "my-infra",
        audit_fn=lambda line: posted.setdefault("line", line),
        forge=f,
        repo="me/infra",
    )

    comments = [c.body for c in f.comments[("me/infra", number)]]
    assert any("sealedstack audit:" in body for body in comments)
    assert "line" not in posted  # posted to the forge, not stderr


def test_break_glass_falls_back_to_stderr_without_a_forge():
    runtime = cfg.Runtime.detect(Path("/repo"), {"USER": "operator"}, git=_git)
    lines = []
    runtime.guard_write(
        "apply",
        "my-infra",
        break_glass=True,
        confirm_fn=lambda prompt: "my-infra",
        audit_fn=lines.append,
    )
    assert lines and "sealedstack audit:" in lines[0]
