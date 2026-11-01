"""FR-3, FR-4, FR-5: fork PRs, dispatched commands, and no YAML parsing on a bare runner."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

from sealedstack import ciexport, config as cfg, infraci
from test_infra_ci import SHA, SLUG, comment_event, ev, make_config, make_forge, runner_for

REPO = Path(__file__).resolve().parents[2]


# -- FR-3: comment commands on fork pull requests -------------------------------------
def fork_pr(f, number=9, head_repo="mallory/my-infra"):
    f.pull_requests[(SLUG, number)] = {"number": number, "state": "open", "merged": False, "head_repo": head_repo, "head_ref": "evil"}
    return number


def test_check_plan_and_status_on_a_fork_pr_are_refused_and_nothing_runs(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    number = fork_pr(f)
    for command in ("check", "plan", "status"):
        res = infraci.resolve(comment_event(f"/sealedstack {command}", actor="writer", number=number), f, c)
        assert not res.proceed and res.checkout_ref == "" and res.command == ""
    bodies = [cm.body for cm in f.comments[(SLUG, number)]]
    assert any("not run on a pull request from a fork (mallory/my-infra)" in b for b in bodies)
    assert all("secrets" in b and "Copy the branch into this repository" in b for b in bodies)


def test_every_other_command_on_a_fork_pr_is_refused_too(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    number = fork_pr(f)
    for command in ("drift", "apply", "up", "restore", "drill", "add-app", "add-server", "reinstall"):
        assert not infraci.resolve(comment_event(f"/sealedstack {command}", actor="you", number=number), f, c).proceed


def test_an_unknown_pull_request_fails_closed(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack check", actor="writer", number=44), f, c)  # not in the forge
    assert not res.proceed and "unknown repository" in f.comments[(SLUG, 44)][0].body


def test_a_pull_request_from_this_repository_still_checks_out_its_merge_ref(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack check", actor="writer"), f, c)
    assert res.proceed and res.checkout_ref == "refs/pull/7/merge"


def test_a_plain_issue_comment_is_not_a_pull_request_and_is_unaffected(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    res = infraci.resolve(comment_event("/sealedstack status", actor="writer", number=3, is_pr=False), f, c)
    assert res.proceed and res.checkout_ref == ""


def test_approve_on_a_fork_pr_that_was_merged_still_works_it_is_main_by_then(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    number = fork_pr(f)
    f.pull_requests[(SLUG, number)].update(merged=True, merge_commit_sha=SHA)
    res = infraci.resolve(comment_event("/sealedstack approve", actor="you", number=number), f, c)
    assert res.proceed and res.command == "apply"


def test_automatic_plans_on_fork_prs_stay_skipped(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    event = ev("pull_request", {"action": "opened", "pull_request": {"number": 9, "head": {"ref": "x", "repo": {"full_name": "mallory/my-infra"}}}})
    assert not infraci.resolve(event, f, c).proceed


# -- FR-4: dispatched commands ----------------------------------------------------------------
def test_a_dispatched_command_outside_the_allow_list_is_refused(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    for bad in ("rm -rf /", "plan; id", "plan\napply", "$(id)", "approve", "server add", "`x`", "../../x", "APPLY", " plan"):
        res = infraci.resolve(ev("workflow_dispatch", actor="you", command=bad), f, c)
        assert not res.proceed and res.command == "", bad
        assert "is not a sealedstack command" in res.note
        assert "\n" not in res.note and "`" not in res.note.split("(allowed")[0]


def test_every_listed_command_still_dispatches(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    for command in sorted(infraci.DISPATCH_COMMANDS):
        assert infraci.resolve(ev("workflow_dispatch", actor="you", command=command), f, c).command == command
    assert infraci.DISPATCH_COMMANDS == {"setup", "check", "plan", "apply", "up", "add-server", "add-app", "reinstall", "status", "restore", "drill", "drift", "verify"}


def test_the_outputs_file_only_ever_carries_a_known_command(tmp_path):
    f, c = make_forge(), make_config(tmp_path)
    out = infraci.resolve(ev("workflow_dispatch", actor="you", command="plan\nwrite=true"), f, c).outputs()
    assert out["proceed"] == "false" and "\n" not in "".join(out.values())


def jobs():
    return yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))["jobs"]


def test_no_job_output_is_interpolated_into_a_shell_script():
    pattern = re.compile(r"\$\{\{\s*(needs|steps|inputs|github\.event|github\.head_ref)[^}]*\}\}")
    offenders = []
    for workflow in ("infra.yml", "app.yml"):
        data = yaml.safe_load((REPO / ".github" / "workflows" / workflow).read_text(encoding="utf-8"))
        for job_name, job in data["jobs"].items():
            for step in job.get("steps", []):
                for match in pattern.finditer(step.get("run", "")):
                    offenders.append(f"{workflow}:{job_name}:{step.get('name') or step.get('id')}: {match.group(0)}")
    assert offenders == [], offenders


def test_the_approve_and_summary_steps_use_env_for_the_command():
    all_jobs = jobs()
    approve = all_jobs["approve"]["steps"][0]
    assert approve["env"]["COMMAND"] == "${{ needs.resolve.outputs.command }}" and approve["run"] == 'echo "Approved ${COMMAND}"'
    summary = all_jobs["run"]["steps"][-1]
    assert summary["env"]["COMMAND"] == "${{ needs.resolve.outputs.command }}" and "${COMMAND}" in summary["run"]


# -- FR-5: no PyYAML assumed on the runner ----------------------------------------------------------
def test_infra_yml_never_imports_yaml_on_the_runner():
    for job in jobs().values():
        for step in job.get("steps", []):
            assert "import yaml" not in step.get("run", ""), step.get("name")


def test_the_tailscale_settings_come_from_the_engine_image():
    steps = jobs()["run"]["steps"]
    names = [s.get("name", "") for s in steps]
    cfg_step = steps[names.index("Read the Tailscale trust settings from config")]
    assert cfg_step["id"] == "ts_cfg" and "debug ci-export --tailscale-oidc" in cfg_step["run"] and "docker run" in cfg_step["run"]
    trust = steps[names.index("Tailscale trust (OIDC workload identity, else the OAuth client)")]
    assert trust["env"]["CFG_CLIENT_ID"] == "${{ steps.ts_cfg.outputs.client_id }}"
    assert trust["env"]["CFG_AUDIENCE"] == "${{ steps.ts_cfg.outputs.audience }}"
    assert "python" not in trust["run"]
    assert names.index("Read the Tailscale trust settings from config") < names.index("Tailscale trust (OIDC workload identity, else the OAuth client)")


def test_the_engine_exports_the_oidc_settings_as_step_outputs(tmp_path):
    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump({"name": "x", "providers": {"mesh_tailscale": {"oauth_client_id": "client-1", "audience": "aud-1"}}}))
    (cdir / "servers.yaml").write_text("servers: {}\n")
    (cdir / "apps.yaml").write_text("apps: {}\n")
    config = cfg.load_config(cdir, REPO, {})
    out = tmp_path / "ghout"
    assert ciexport.export_tailscale_oidc(config, out) == 0
    assert out.read_text() == "client_id=client-1\naudience=aud-1\n"
    # unset values are empty, not missing, so the workflow's fallback logic is simple
    config.stack["providers"] = {}
    out2 = tmp_path / "ghout2"
    ciexport.export_tailscale_oidc(config, out2)
    assert out2.read_text() == "client_id=\naudience=\n"
    config.stack["providers"] = {"mesh_tailscale": {"oauth_client_id": "a\nb=c", "audience": "x"}}
    assert ciexport.export_tailscale_oidc(config, tmp_path / "ghout3") == 1  # no output-file injection


def test_the_cli_entry_point_accepts_the_new_flag(tmp_path):
    import io
    import contextlib

    cdir = tmp_path / "config"
    cdir.mkdir()
    (cdir / "stack.yaml").write_text(yaml.safe_dump({"name": "x", "providers": {"mesh_tailscale": {"oauth_client_id": "c", "audience": "a"}}}))
    (cdir / "servers.yaml").write_text("servers: {}\n")
    (cdir / "apps.yaml").write_text("apps: {}\n")
    import os

    old = os.environ.get("SEALEDSTACK_CONFIG")
    os.environ["SEALEDSTACK_CONFIG"] = str(cdir)
    try:
        assert ciexport.main(["--tailscale-oidc", "--output", str(tmp_path / "o")]) == 0
    finally:
        if old is None:
            del os.environ["SEALEDSTACK_CONFIG"]
        else:
            os.environ["SEALEDSTACK_CONFIG"] = old
    assert (tmp_path / "o").read_text() == "client_id=c\naudience=a\n"


def test_app_yml_installs_a_pinned_pyyaml_before_any_step_parses_yaml():
    data = yaml.safe_load((REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8"))
    for job_name, job in data["jobs"].items():
        steps = job.get("steps", [])
        first_yaml_use = next((i for i, s in enumerate(steps) if "import yaml" in s.get("run", "") or "composelint" in s.get("name", "")), None)
        if first_yaml_use is None:
            continue
        installs = [i for i, s in enumerate(steps) if 'pip install "pyyaml==' in s.get("run", "")]
        assert installs and installs[0] < first_yaml_use, f"job {job_name} parses YAML without installing a pinned PyYAML first"
        python_at = next(i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/setup-python@"))
        assert python_at < installs[0]
    text = (REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    assert len(re.findall(r'pyyaml==6\.0\.2', text)) == 3  # setup, build and release


def test_the_engine_release_workflow_takes_its_input_through_env_and_validates_it():
    data = yaml.safe_load((REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))
    step = next(s for job in data["jobs"].values() for s in job.get("steps", []) if s.get("id") == "version")
    assert step["env"]["INPUT_VERSION"] == "${{ github.event.inputs.version }}" and "github.event" not in step["run"]
    assert "*[!0-9A-Za-z.+-]*" in step["run"]  # only a plain version reaches later scripts and image tags


def test_the_documented_oidc_subject_matches_a_job_without_an_environment():
    """The release job declares no `environment:` (production is only a GitHub Deployment name), so GitHub's subject for it is
    repo:<owner>/<app>:ref:refs/heads/<branch>. The docs once said `…:environment:production`, which never matches."""
    import re

    workflow = (REPO / ".github" / "workflows" / "app.yml").read_text(encoding="utf-8")
    assert not re.search(r"^\s{4}environment:", workflow, re.M)  # no job-level environment
    for doc in ("docs/ci-cd.md", "docs/user-experience.md", "engine/sealedstack/setupjob.py"):
        assert ":environment:production" not in (REPO / doc).read_text(encoding="utf-8"), doc


def test_a_tailscale_join_with_an_audience_uses_the_action_version_that_has_that_input():
    """Live run 2026-11-01: `tailscale/github-action@v3` has no `audience` input; the OIDC join failed with 'OAuth identity empty'."""
    for name in ("app.yml", "infra.yml", "drift.yml"):
        data = yaml.safe_load((REPO / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        for job in (data.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                uses = str(step.get("uses") or "")
                if uses.startswith("tailscale/github-action@") and "audience" in (step.get("with") or {}):
                    assert uses.split("@", 1)[1].startswith("v4"), f"{name}: {step.get('name')} uses {uses}"
