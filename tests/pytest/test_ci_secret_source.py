"""DR-6 (D-050): `secrets.source: ci` works in CI.

The reusable workflow forwards the caller's secrets as one JSON variable
(SEALEDSTACK_CI_SECRETS = toJSON(secrets)); CiSource reads it, then plain env vars.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sealedstack import preflight, secrets as secrets_mod
from sealedstack.secrets import CI_SECRETS_VAR, CiSource, build_source, parse_ci_secrets
from test_engine_preflight import NEEDED, STACK, by_id, make_config, make_ctx

REPO = Path(__file__).resolve().parents[2]


def blob(**values):
    return json.dumps(values)


# -- parsing -------------------------------------------------------------------------
def test_valid_json_is_read():
    parsed, problem = parse_ci_secrets(blob(CLOUDFLARE_API_TOKEN="t1", github_token="ghs_x"))
    assert parsed == {"CLOUDFLARE_API_TOKEN": "t1", "github_token": "ghs_x"} and problem == ""


def test_empty_and_missing_are_fine_and_not_a_problem():
    for raw in (None, "", "   ", "{}"):
        assert parse_ci_secrets(raw) == ({}, "")


def test_junk_is_reported_without_echoing_it():
    for raw, text in (("not json {", "not valid JSON"), ('["a"]', "not a JSON object"), ('"just a string"', "not a JSON object")):
        parsed, problem = parse_ci_secrets(raw)
        assert parsed == {} and text in problem and raw not in problem


def test_non_string_and_empty_values_are_dropped():
    parsed, _ = parse_ci_secrets(json.dumps({"A": "x", "B": "", "C": 5, "D": None, "E": {"n": 1}, "F": ["l"]}))
    assert parsed == {"A": "x"}


def test_multiline_and_special_characters_survive():
    value = "-----BEGIN KEY-----\nline \"two\"\\n\n-----END KEY-----\n"
    assert parse_ci_secrets(blob(SSH=value))[0]["SSH"] == value


# -- the source ------------------------------------------------------------------------
def test_the_json_wins_and_plain_environment_variables_are_the_fallback():
    source = CiSource({CI_SECRETS_VAR: blob(A="from-json"), "A": "from-env", "B": "only-env"})
    assert source.get("A").value == "from-json"
    assert source.get("B").value == "only-env" and source.get("B").found
    assert source.kind == "ci"


def test_a_secret_that_was_not_forwarded_says_how_to_fix_it():
    result = CiSource({CI_SECRETS_VAR: blob(A="x")}).get("MISSING")
    assert result.missing and "secrets: inherit" in result.detail and "repository secret" in result.detail


def test_a_broken_blob_is_explained_when_a_lookup_misses_and_the_env_still_works():
    source = CiSource({CI_SECRETS_VAR: "{oops", "X": "v"})
    assert source.get("X").found
    miss = source.get("Y")
    assert miss.missing and "not valid JSON" in miss.detail and "oops" not in miss.detail


def test_build_source_picks_it_for_slots_secrets_ci(tmp_path):
    stack = {"slots": {"secrets": "ci"}}
    assert isinstance(build_source(stack, tmp_path, {CI_SECRETS_VAR: blob(A="1")}), CiSource)


def test_the_blob_never_appears_in_a_repr_or_an_error():
    source = CiSource({CI_SECRETS_VAR: blob(TOKEN="super-secret-value-123")})
    assert "super-secret-value-123" not in repr(source.get("MISSING")) and "super-secret-value-123" not in source.get("MISSING").detail


# -- check works with source: ci ----------------------------------------------------------
def ci_stack():
    return {**STACK, "slots": {**STACK["slots"], "secrets": "ci"}}


def test_check_secrets_reads_everything_from_the_forwarded_json(tmp_path):
    config = make_config(tmp_path, stack=ci_stack())
    env = {CI_SECRETS_VAR: json.dumps(NEEDED)}
    source = build_source(config.stack, config.config_dir, env)
    results = by_id(preflight.check_secrets(make_ctx(tmp_path, config, env=env, secrets=source)))
    assert results and all(r.status == "ok" for r in results.values()), {k: (v.status, v.detail) for k, v in results.items() if v.status != "ok"}
    # the values themselves are never in a result
    assert not any(value in r.detail + r.fix for r in results.values() for value in NEEDED.values() if len(value) > 4)


def test_check_secrets_names_the_fix_when_the_caller_did_not_pass_secrets(tmp_path):
    config = make_config(tmp_path, stack=ci_stack())
    env = {CI_SECRETS_VAR: json.dumps({"github_token": "ghs_x"})}  # what a caller without `secrets: inherit` forwards
    source = build_source(config.stack, config.config_dir, env)
    results = by_id(preflight.check_secrets(make_ctx(tmp_path, config, env=env, secrets=source)))
    row = results["secrets.hcloud_token"]
    assert row.status == "fail" and "HCLOUD_TOKEN" in row.fix


# -- the workflow --------------------------------------------------------------------------
def workflow():
    return yaml.safe_load((REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8"))


def _all_steps():
    return [(job, step) for job, body in workflow()["jobs"].items() for step in body.get("steps", [])]


def test_the_bundle_reaches_only_the_ci_source_exec_step_and_is_not_interpolated_into_a_script():
    steps = workflow()["jobs"]["run"]["steps"]
    ci_exec = next(s for s in steps if str(s.get("name", "")).startswith("sealedstack ") and "(secret source ci)" in s["name"])
    assert ci_exec["env"][CI_SECRETS_VAR] == "${{ toJSON(secrets) }}"
    assert "-e SEALEDSTACK_CI_SECRETS" in ci_exec["run"]  # by name: the value is never on the command line
    assert "toJSON" not in ci_exec["run"] and "secrets." not in ci_exec["run"]
    assert ci_exec["if"] == "needs.resolve.outputs.ci_secrets == 'true'"


def test_toJSON_secrets_appears_only_in_steps_gated_on_ci_secrets_true():
    holders = [(job, s) for job, s in _all_steps() if "toJSON(secrets)" in json.dumps(s)]
    assert len(holders) == 2  # the mask step and the ci-source exec step, nothing else
    for job, step in holders:
        assert job == "run"
        assert "needs.resolve.outputs.ci_secrets == 'true'" in step["if"], step.get("name")
    lines = (REPO / ".github" / "workflows" / "infra.yml").read_text(encoding="utf-8").splitlines()
    assert sum(1 for line in lines if "toJSON(secrets)" in line and not line.lstrip().startswith("#")) == 2


def test_the_resolve_job_never_receives_the_bundle():
    resolve_steps = workflow()["jobs"]["resolve"]["steps"]
    assert "SEALEDSTACK_CI_SECRETS" not in json.dumps(resolve_steps) and "toJSON" not in json.dumps(resolve_steps)
    assert workflow()["jobs"]["resolve"]["outputs"]["ci_secrets"] == "${{ steps.resolve.outputs.ci_secrets || 'false' }}"


def test_the_non_ci_path_exec_step_passes_only_the_source_token_and_gh_token():
    steps = workflow()["jobs"]["run"]["steps"]
    plain = next(s for s in steps if str(s.get("name", "")).startswith("sealedstack ") and "(secret source ci)" not in s["name"])
    assert plain["if"] == "needs.resolve.outputs.ci_secrets != 'true'"
    assert CI_SECRETS_VAR not in plain["env"] and "SEALEDSTACK_CI_SECRETS" not in plain["run"]
    assert "toJSON" not in json.dumps(plain)
    assert plain["env"]["BWS_ACCESS_TOKEN"] == "${{ secrets.BWS_ACCESS_TOKEN }}"
    assert plain["env"]["GH_TOKEN"] == "${{ secrets.GH_TOKEN || github.token }}"
    others = {v for v in plain["env"].values() if "secrets." in str(v)}
    # the secret source's access token (Bitwarden's, or Doppler's service tokens, D-066) and
    # GH_TOKEN; never the whole repository secrets bundle
    assert others == {
        "${{ secrets.BWS_ACCESS_TOKEN }}",
        "${{ secrets.DOPPLER_TOKEN }}",
        "${{ secrets.DOPPLER_TOKEN_APPS }}",
        "${{ secrets.GH_TOKEN || github.token }}",
    }


def test_exactly_one_exec_step_runs_for_any_ci_secrets_value():
    steps = workflow()["jobs"]["run"]["steps"]
    execs = [s for s in steps if "ci exec" in s.get("run", "")]
    assert len(execs) == 2
    assert {s["if"] for s in execs} == {"needs.resolve.outputs.ci_secrets == 'true'", "needs.resolve.outputs.ci_secrets != 'true'"}
    # the two copies differ only in how the secrets arrive: the bundle (ci) or the source's own
    # token (Doppler's service tokens, D-066; Bitwarden's is passed by both)
    strip = lambda s: s["run"].replace(" -e SEALEDSTACK_CI_SECRETS", "").replace(" -e DOPPLER_TOKEN -e DOPPLER_TOKEN_APPS", "")
    assert strip(execs[0]) == strip(execs[1])


# -- resolve outputs ci_secrets from stack.yaml (slots.secrets) --------------------------------
@pytest.mark.parametrize(
    "slots, expected",
    [({"secrets": "bitwarden"}, "false"), ({"secrets": "ci"}, "true"), ({"secrets": "sops"}, "false"), ({}, "false"),
     ({"secrets": "env"}, "false"), ({"secrets": " CI "}, "true")],
)
def test_resolve_outputs_ci_secrets_per_source(tmp_path, slots, expected):
    from sealedstack import infraci

    config = make_config(tmp_path, stack={"name": "x", "slots": slots})
    res = infraci.Resolution(command="check", ci_secrets=infraci.ci_secrets_enabled(config))
    assert res.outputs()["ci_secrets"] == expected


def test_ci_secrets_is_false_without_a_config_and_when_nothing_runs():
    from sealedstack import infraci

    assert infraci.ci_secrets_enabled(None) is False
    assert infraci.Resolution().outputs()["ci_secrets"] == "false"


def test_ci_resolve_writes_ci_secrets_to_the_job_output_file(tmp_path):
    from sealedstack import infraci, config as config_mod
    import yaml as _yaml

    for source, expected in (("ci", "true"), ("bitwarden", "false"), (None, "false")):
        root = tmp_path / str(source)
        (root / "config").mkdir(parents=True)
        slots = {"secrets": source} if source else {}
        (root / "config" / "stack.yaml").write_text(_yaml.safe_dump({"name": "x", "slots": slots}))
        (root / "config" / "servers.yaml").write_text("servers: {}\n")
        (root / "config" / "apps.yaml").write_text("apps: {}\n")
        out = root / "ghout"
        env = {"SEALEDSTACK_CONFIG": str(root / "config"), "GITHUB_EVENT_NAME": "schedule", "GITHUB_REPOSITORY": "you/my-infra",
               "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "a" * 40, "GITHUB_ACTOR": "you", "SEALEDSTACK_DEFAULT_BRANCH": "main"}
        forge = FakeForgeStub()
        rc = infraci.main(["resolve", "--output", str(out)], env=env, forge=forge)
        assert rc == 0
        assert f"ci_secrets={expected}" in out.read_text()


class FakeForgeStub:
    """resolve for a schedule event never calls the forge; any call is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"resolve called forge.{name}")


def test_every_forwarded_value_is_registered_as_a_mask_before_the_engine_runs():
    steps = workflow()["jobs"]["run"]["steps"]
    names = [str(s.get("name", "")) for s in steps]
    mask_at = names.index("Mask the forwarded secrets")
    assert mask_at < next(i for i, n in enumerate(names) if n.startswith("sealedstack "))
    script = steps[mask_at]["run"]
    assert "::add-mask::" in script and "splitlines()" in script  # each line of a multi-line value
    assert "import yaml" not in script  # stdlib only
    assert steps[mask_at]["env"][CI_SECRETS_VAR] == "${{ toJSON(secrets) }}"


def run_mask_script(raw):
    import subprocess
    import sys

    script = workflow()["jobs"]["run"]["steps"][
        [str(s.get("name", "")) for s in workflow()["jobs"]["run"]["steps"]].index("Mask the forwarded secrets")
    ]["run"].split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    return subprocess.run([sys.executable, "-c", script], env={"SEALEDSTACK_CI_SECRETS": raw}, capture_output=True, text=True)


def test_the_mask_script_masks_values_and_each_line_and_prints_nothing_else():
    done = run_mask_script(json.dumps({"A": "alpha-value", "KEY": "line-one\nline-two", "EMPTY": "", "N": 3}))
    assert done.returncode == 0
    assert done.stdout.splitlines() == ["::add-mask::alpha-value", "::add-mask::line-one", "::add-mask::line-two"]
    assert run_mask_script("garbage").stdout == "" and run_mask_script("").returncode == 0


def test_prerequisites_explain_secrets_inherit_and_the_repository_secret_limit():
    text = (REPO / "docs" / "prerequisites.md").read_text(encoding="utf-8")
    assert "SEALEDSTACK_CI_SECRETS" in text and "secrets: inherit" in text and "GitHub Free" in text
