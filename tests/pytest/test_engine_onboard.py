"""App-side onboarding: the compose transformer and the generated change set (UX section 6)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
for extra in (REPO / "engine", REPO / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import lint_compose  # noqa: E402
import validate  # noqa: E402
from sealedstack import onboard  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "plain-app"
GOLDEN = REPO / "tests" / "golden" / "onboarding"

REGISTRY = "ghcr.io/example-owner"


def read_tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def transform(text: str, **kwargs):
    defaults = dict(name="example-app", repo_slug="example-app", registry_prefix=REGISTRY)
    defaults.update(kwargs)
    return onboard.transform_compose(text, **defaults)


def generate(files=None, **kwargs):
    defaults = dict(
        name="example-app",
        repo="example-owner/example-app",
        registry_prefix=REGISTRY,
        tool_owner="example-owner",
        version="0.5.0",
    )
    defaults.update(kwargs)
    return onboard.generate(files if files is not None else read_tree(FIXTURE), **defaults)


# --------------------------------------------------------------------------- #
# golden snapshot of the whole change set for a plain app
# --------------------------------------------------------------------------- #
def snapshot(plan) -> dict[str, str]:
    out = {f"files/{path}": text for path, text in plan.files.items()}
    out["deletes.txt"] = "\n".join(plan.deletes) + "\n"
    out["pr-body.md"] = plan.body
    return out


def test_onboarding_matches_the_golden_snapshot():
    actual = snapshot(generate())
    if os.environ.get("UPDATE_GOLDEN"):
        for path, text in actual.items():
            target = GOLDEN / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    expected = read_tree(GOLDEN)
    assert set(actual) == set(expected), "golden file set differs (UPDATE_GOLDEN=1 to regenerate)"
    for path in expected:
        assert actual[path] == expected[path], f"{path} differs from the golden snapshot (UPDATE_GOLDEN=1 to regenerate)"


def test_generated_files_pass_sealedstack_validation():
    plan = generate()
    # the manifest validates against the manifest schema
    assert validate.schema_errors(yaml.safe_load(plan.files["sealedstack.yaml"]), validate.SCHEMAS / "sealedstack.schema.json") == []
    compose = yaml.safe_load(plan.files["docker-compose.yml"])
    needs = plan.manifest["needs"]
    assert lint_compose.lint(compose, needs) == []
    assert lint_compose.lint_build(compose, "ci", registry="ghcr.io", owner="example-owner", repo="example-app") == []


def test_override_restores_ports_and_database_for_development():
    plan = generate()
    override_images = yaml.safe_load(plan.files["docker-compose.override.yml"])["services"]
    assert override_images["ui"]["image"] == "ghcr.io/example-owner/example-app-ui:${APP_VERSION:-dev}"
    assert override_images["api"]["image"] == "ghcr.io/example-owner/example-app-api:${APP_VERSION:-dev}"
    base = yaml.safe_load(plan.files["docker-compose.yml"])
    override = yaml.safe_load(plan.files["docker-compose.override.yml"])
    original = yaml.safe_load((FIXTURE / "docker-compose.yml").read_text(encoding="utf-8"))
    # a developer's `docker compose up` merges base + override: the database and
    # the published ports are back
    assert "postgres" not in base["services"]
    assert override["services"]["postgres"] == original["services"]["postgres"]
    assert override["services"]["ui"]["ports"] == ["8080:80"]
    assert override["services"]["api"]["ports"] == ["3000:3000"]
    assert override["services"]["api"]["environment"]["DATABASE_URL"] == original["services"]["api"]["environment"]["DATABASE_URL"]
    assert override["services"]["api"]["depends_on"] == original["services"]["api"]["depends_on"]
    assert override["volumes"] == original["volumes"]


def test_replaced_workflow_and_its_script_are_deleted():
    plan = generate()
    assert plan.deletes == [".github/workflows/deploy.yml", "scripts/deploy.sh"]
    release = plan.files[".github/workflows/release.yml"]
    assert "example-owner/sealedstack/.github/workflows/app.yml@v0.5.0" in release
    assert "workflow_dispatch" in release and "tag:" in release  # manual release kept


def test_a_script_still_used_by_another_workflow_is_kept():
    files = read_tree(FIXTURE)
    files[".github/workflows/other.yml"] = "jobs:\n  x:\n    steps:\n      - run: scripts/deploy.sh now\n"
    assert generate(files).deletes == [".github/workflows/deploy.yml"]


def test_release_template_matches_the_reference_app():
    reference = (REPO / "examples" / "app-template" / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    generated = onboard.render_release_workflow("<owner>", "0.5.0")
    # the same workflow; only the explanatory comment at the top differs
    assert generated[generated.index("name: release") :] == reference[reference.index("name: release") :]
    assert "<owner>" not in onboard.render_release_workflow("example-owner", "0.5.0")


# --------------------------------------------------------------------------- #
# compose transformer cases
# --------------------------------------------------------------------------- #
def test_compose_without_ports_only_gains_the_image():
    text = "services:\n  web:\n    build: .\n    restart: unless-stopped\n    expose:\n      - \"3000\"\n"
    result = transform(text, web_port=0)
    assert result.web_port == 3000
    assert result.removed.ports == {}
    assert result.text == (
        "services:\n  web:\n    build: .\n"
        "    image: ghcr.io/example-owner/example-app-web:${APP_VERSION}\n"
        "    restart: unless-stopped\n    expose:\n      - \"3000\"\n"
    )
    assert len(result.changes) == 1 and "added `image:" in result.changes[0]
    plan = generate({"docker-compose.yml": text})
    # the only thing a developer needs is a valid tag for the pinned image
    override = yaml.safe_load(plan.files["docker-compose.override.yml"])
    assert override["services"] == {"web": {"image": "ghcr.io/example-owner/example-app-web:${APP_VERSION:-dev}"}}


def test_multiple_services_all_lose_ports_and_the_first_is_the_web_service():
    text = (
        "services:\n"
        "  api:\n    build: ./api\n    ports: [\"3000:3000\"]\n"
        "  ui:\n    build: ./ui\n    ports:\n      - 8080:80\n      - \"8443:443\"\n"
    )
    result = transform(text)
    assert yaml.safe_load(result.text)["services"]["api"].keys() == {"build", "image"}
    assert "ports" not in yaml.safe_load(result.text)["services"]["ui"]
    assert result.web_service == "api" and result.web_port == 3000
    assert result.removed.ports == {"api": ["3000:3000"], "ui": ["8080:80", "8443:443"]}
    assert any("only `api` is routed" in n for n in result.notes)
    # the web service can be chosen explicitly
    chosen = transform(text, web_service="ui")
    assert chosen.web_service == "ui" and chosen.web_port == 80


def test_named_postgres_service_is_removed_and_declared_as_a_need():
    text = (
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "    ports: [\"5000:5000\"]\n"
        "    environment:\n"
        "      - DB_URL=postgres://u:p@database:5432/shop\n"
        "      - MODE=prod\n"
        "    depends_on: [database]\n"
        "  database:\n"
        "    image: postgres:15.4\n"
        "    environment:\n"
        "      POSTGRES_DB: shopdb\n"
        "    volumes:\n"
        "      - dbdata:/var/lib/postgresql/data\n"
        "volumes:\n"
        "  dbdata:\n"
        "  uploads:\n"
    )
    result = transform(text)
    parsed = yaml.safe_load(result.text)
    assert list(parsed["services"]) == ["web"]
    assert "depends_on" not in parsed["services"]["web"]
    assert parsed["volumes"] == {"uploads": None}  # only the database volume went
    assert result.needs.postgres == {"version": "15.4", "database": "shopdb", "env": "DB_URL"}
    assert "- DB_URL=${DB_URL}" in result.text and "MODE=prod" in result.text
    assert result.removed.services["database"][0].startswith("  database:")


def test_redis_service_becomes_a_need():
    text = (
        "services:\n  web:\n    build: .\n    ports: ['80:80']\n"
        "    environment:\n      REDIS_URL: redis://redis:6379/0\n"
        "  redis:\n    image: redis:7-alpine\n"
    )
    result = transform(text)
    assert result.needs.redis == {"env": "REDIS_URL", "version": "7"}
    assert "redis" not in yaml.safe_load(result.text)["services"]
    assert "REDIS_URL: ${REDIS_URL}" in result.text


def test_build_only_service_gets_a_pinned_image_and_build_is_kept():
    text = "services:\n  worker:\n    build:\n      context: ./worker\n      args:\n        A: b\n    command: run\n  web:\n    image: nginx:1.27\n    ports: ['80:80']\n"
    result = transform(text)
    parsed = yaml.safe_load(result.text)
    assert parsed["services"]["worker"]["image"] == "ghcr.io/example-owner/example-app-worker:${APP_VERSION}"
    assert parsed["services"]["worker"]["build"]["args"] == {"A": "b"}
    assert parsed["services"]["web"]["image"] == "nginx:1.27"  # no build: left alone
    assert "image: ghcr.io/example-owner/example-app-worker:${APP_VERSION}\n    command: run" in result.text


def test_a_different_pinned_image_on_a_built_service_is_replaced():
    text = "services:\n  web:\n    build: .\n    image: old/name:latest\n    ports: ['80:80']\n"
    result = transform(text)
    assert yaml.safe_load(result.text)["services"]["web"]["image"] == "ghcr.io/example-owner/example-app-web:${APP_VERSION}"
    assert "old/name" not in result.text


def test_comments_and_formatting_survive():
    text = "# top\nservices:\n  web:  # the site\n    build: .   # local build\n    ports:\n      - \"80:80\"\n    restart: always\n"
    result = transform(text)
    assert "# top" in result.text and "# the site" in result.text and "# local build" in result.text
    assert "    restart: always" in result.text


def test_ports_list_at_the_same_indent_as_the_key_is_removed_whole():
    text = "services:\n  web:\n    build: .\n    ports:\n    - \"80:80\"\n    - \"81:81\"\n    restart: always\n"
    result = transform(text)
    assert "ports" not in result.text and "- \"81:81\"" not in result.text
    assert "restart: always" in result.text


def test_secret_looking_variables_are_declared_and_others_are_flagged():
    text = (
        "services:\n  web:\n    build: .\n    ports: ['80:80']\n    environment:\n"
        "      STRIPE_SECRET_KEY: ${STRIPE_SECRET_KEY}\n      REGION: ${REGION}\n      OPT: ${OPT:-x}\n"
    )
    result = transform(text)
    assert result.secrets == ["STRIPE_SECRET_KEY"]
    assert any("`REGION`" in n for n in result.notes)
    manifest_text, manifest = onboard.render_manifest("example-app", "docker-compose.yml", result)
    assert manifest["secrets"] == ["STRIPE_SECRET_KEY"] and "secrets: [STRIPE_SECRET_KEY]" in manifest_text


def test_healthcheck_path_becomes_the_manifest_health():
    plan = generate()
    assert plan.manifest["web"] == {"service": "ui", "port": 80, "health": "/api/health"}
    assert plan.manifest["needs"]["postgres"] == {
        "version": "16",
        "database": "example",
        "env": "DATABASE_URL",
        "backup": {"every": "6h", "keep": 28},
    }


# --------------------------------------------------------------------------- #
# errors say what to do
# --------------------------------------------------------------------------- #
def test_already_onboarded_repo_is_refused():
    files = read_tree(FIXTURE)
    files["sealedstack.yaml"] = "name: x\n"
    with pytest.raises(onboard.OnboardError, match="already has sealedstack.yaml"):
        generate(files)


def test_missing_compose_is_refused_with_guidance():
    with pytest.raises(onboard.OnboardError, match="no compose file"):
        generate({"README.md": "x"})


def test_port_must_be_given_when_nothing_is_published():
    with pytest.raises(onboard.OnboardError, match="--web-port"):
        transform("services:\n  web:\n    build: .\n")
    assert transform("services:\n  web:\n    build: .\n", web_port=8000).web_port == 8000


def test_invalid_yaml_is_refused():
    with pytest.raises(onboard.OnboardError, match="not valid YAML"):
        transform("services: [unclosed\n")


def test_existing_override_is_left_alone_and_flagged():
    files = read_tree(FIXTURE)
    files["docker-compose.override.yml"] = "services: {}\n"
    plan = generate(files)
    assert "docker-compose.override.yml" not in plan.files
    assert any("already exists" in n for n in plan.notes)


# --------------------------------------------------------------------------- #
# `sealedstack debug onboard` on a local checkout
# --------------------------------------------------------------------------- #
def test_main_dry_run_changes_nothing_and_prints_the_pr_body(tmp_path, capsys):
    import shutil

    work = tmp_path / "app"
    shutil.copytree(FIXTURE, work)
    before = read_tree(work)
    rc = onboard.main(["--path", str(work), "--name", "example-app", "--repo", "example-owner/example-app", "--version", "0.5.0"])
    out = capsys.readouterr().out
    assert rc == 0 and read_tree(work) == before
    assert "would write sealedstack.yaml" in out and "would delete scripts/deploy.sh" in out
    assert "This PR prepares example-app for sealedstack." in out


def test_main_write_applies_the_change_set_exactly_as_generated(tmp_path):
    import shutil

    work = tmp_path / "app"
    shutil.copytree(FIXTURE, work)
    rc = onboard.main(
        ["--path", str(work), "--name", "example-app", "--repo", "example-owner/example-app", "--version", "0.5.0", "--write", "--pr-body", str(tmp_path / "body.md")]
    )
    assert rc == 0
    plan = generate()
    for path, text in plan.files.items():
        assert (work / path).read_text(encoding="utf-8") == text
    for path in plan.deletes:
        assert not (work / path).exists()
    assert (tmp_path / "body.md").read_text(encoding="utf-8") == plan.body
    # re-running on the onboarded checkout is refused rather than doing it twice
    assert onboard.main(["--path", str(work), "--name", "example-app", "--repo", "example-owner/example-app"]) == 2


def test_cli_exposes_debug_onboard_with_its_flags():
    from sealedstack import cli

    argv = ["debug", "onboard", "--path", ".", "--name", "x", "--repo", "a/b", "--health", "/api/health"]
    head, tail = cli.split_passthrough(argv)
    args = cli.build_parser().parse_args(head)
    args.extras = tail
    action = cli.plan_action(args, code_root=REPO)
    assert action.argv == [[sys.executable, "-m", "sealedstack.onboard", "--path", ".", "--name", "x", "--repo", "a/b", "--health", "/api/health"]]


def test_removals_do_not_leave_runs_of_blank_lines_and_trailing_comments_stay():
    text = (
        "services:\n  web:\n    build: .\n    ports: ['80:80']\n\n"
        "  db:\n    image: postgres:16\n    volumes:\n      - data:/var/lib/postgresql/data\n\n"
        "volumes:\n  data:\n# keep me\n"
    )
    result = transform(text)
    assert "\n\n\n" not in result.text
    assert result.text.endswith("# keep me\n")
    assert "volumes" not in yaml.safe_load(result.text)
