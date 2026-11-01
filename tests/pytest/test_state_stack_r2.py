"""First live `up`: state-init made the R2 bucket, then died on PutBucketVersioning (R2 answers 501)."""
from __future__ import annotations

from pathlib import Path

from sealedstack import stateinit
from sealedstack.secrets import EnvSource

from test_engine_server import REPO, make_config

STATE_TF = (REPO / "tofu" / "stacks" / "state" / "main.tf").read_text(encoding="utf-8")


def test_the_state_stack_does_not_ask_r2_for_versioning():
    code = "\n".join(line for line in STATE_TF.splitlines() if not line.lstrip().startswith("#"))
    assert "aws_s3_bucket_versioning" not in code


def test_an_existing_bucket_is_adopted_before_the_apply(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "state_passphrase": "0123456789abcdef"})
    calls = []
    assert stateinit.run(config, {"GITHUB_ACTIONS": "true"}, source=source, runner=lambda argv: calls.append(argv) or 0) == 0
    verbs = [c[3] for c in calls]
    assert verbs == ["init", "import", "apply"]
    assert calls[1][-2:] == ["aws_s3_bucket.state", "sealedstack-state"]
    assert "-auto-approve" in calls[2]


def test_a_failed_import_means_the_bucket_is_new_and_does_not_stop_the_run(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "state_passphrase": "0123456789abcdef"})
    calls = []

    def runner(argv):
        calls.append(argv)
        return 1 if argv[3] == "import" else 0

    assert stateinit.run(config, {}, source=source, runner=runner) == 0
    assert [c[3] for c in calls] == ["init", "import", "apply"]


def test_a_failed_init_or_apply_still_fails_the_run(tmp_path):
    config = make_config(tmp_path)
    source = EnvSource({"R2_ACCESS_KEY_ID": "a", "R2_SECRET_ACCESS_KEY": "b", "state_passphrase": "0123456789abcdef"})
    assert stateinit.run(config, {}, source=source, runner=lambda argv: 1 if argv[3] == "init" else 0) == 1
    assert stateinit.run(config, {}, source=source, runner=lambda argv: 1 if argv[3] == "apply" else 0) == 1


def test_no_stack_chooses_between_differently_typed_decoded_files():
    """Live run 2026-11-01: `fileexists(f) ? jsondecode(file(f)) : {}` fails with "Inconsistent conditional result types"
    once the file has content; decode one chosen STRING instead (the same shape bug broke the tunnel ingress)."""
    import re

    offenders = []
    for path in (REPO / "tofu").rglob("*.tf"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\?\s*(json|yaml)decode\(", code):
                offenders.append(f"{path.relative_to(REPO)}:{number}")
    assert offenders == []
