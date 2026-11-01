"""D-053 guard: nothing in code may make an artifact public.

Static scan of ``.github/workflows/*.yml``, ``scripts/``, ``engine/`` and the
``justfile`` for anything that would change visibility or publish to a public
destination:

* ``gh repo edit ... --visibility public``, ``visibility: public`` in any form;
* package-visibility API calls;
* ``actions/deploy-pages`` (GitHub Pages);
* Homebrew tap publishing;
* publishing to public package registries (PyPI, npm, crates.io);
* a ``docker/build-push-action`` that pushes anywhere but the owner's own GHCR
  namespace (which inherits the repository's private visibility).

A workflow step may only do such a thing when its ``if`` (or its job's ``if``)
contains **both** ``!github.event.repository.private`` **and**
``vars.PUBLIC_RELEASE_CONFIRMED == 'true'`` (an owner-set repository variable
that does not exist today, so nothing public can run). Code under ``scripts/``,
``engine/`` and the ``justfile`` has no gate to hide behind: any hit fails.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]

GATE_PRIVATE = "!github.event.repository.private"
GATE_CONFIRMED = "vars.PUBLIC_RELEASE_CONFIRMED == 'true'"

# Patterns that make something public, matched against step text / source text.
PUBLIC_PATTERNS = {
    "visibility flag": re.compile(r"--visibility[ =]+[\"']?public", re.I),
    "visibility public": re.compile(r"[\"']?visibility[\"']?\s*[:=]\s*[\"']?public", re.I),
    "package visibility API": re.compile(r"packages[^\n]*visibility|visibility[^\n]*/packages", re.I),
    "public repo create": re.compile(r"gh\s+repo\s+create[^\n]*--public", re.I),
    "github pages": re.compile(r"actions/deploy-pages|\bgh-pages\b|peaceiris/actions-gh-pages", re.I),
    "homebrew tap": re.compile(
        r"bump-homebrew|brew\s+(tap-new|create|bump)|homebrew[-_ ]?(tap|core|formula)\b|\bhomebrew-[\w-]+",
        re.I,
    ),
    "public registry publish": re.compile(
        r"pypa/gh-action-pypi-publish|\bnpm\s+publish|\btwine\s+upload|\bcargo\s+publish", re.I
    ),
}

OWNER_NAMESPACE = "ghcr.io/${{ github.repository_owner }}/"


def step_text(step: dict) -> str:
    """The parts of a workflow step that execute (comments are not in the YAML)."""
    parts = [str(step.get("uses", "")), str(step.get("run", ""))]
    parts.append(json.dumps(step.get("with") or {}, default=str))
    parts.append(json.dumps(step.get("env") or {}, default=str))
    return "\n".join(parts)


def gated(*conditions: object) -> bool:
    text = " ".join(str(c) for c in conditions if c)
    return GATE_PRIVATE in text and GATE_CONFIRMED in text


def workflow_violations(name: str, text: str) -> list[str]:
    problems: list[str] = []
    data = yaml.safe_load(text) or {}
    # `on:` parses as the boolean True in YAML 1.1.
    triggers = data.get("on", data.get(True)) or {}
    trigger_names = {triggers} if isinstance(triggers, str) else set(triggers)
    # A workflow that is only ever called (`workflow_call`) runs in the *caller's*
    # repository (an app repo pushing to that user's own configured registry), not
    # in this one, so the owner-namespace rule applies to the others only. The
    # public-artifact patterns apply to every workflow.
    runs_here = trigger_names != {"workflow_call"}
    for job_id, job in (data.get("jobs") or {}).items():
        job = job or {}
        job_if = job.get("if")
        for index, step in enumerate(job.get("steps") or []):
            body = step_text(step)
            label = f"{name}: job '{job_id}' step {index + 1} ({step.get('name') or step.get('uses') or 'run'})"
            hits = [kind for kind, pattern in PUBLIC_PATTERNS.items() if pattern.search(body)]
            if hits and not gated(job_if, step.get("if")):
                problems.append(
                    f"{label}: {', '.join(hits)} without both {GATE_PRIVATE} and {GATE_CONFIRMED}"
                )
            if runs_here:
                problems.extend(f"{label}: {p}" for p in push_problems(step, job_if))
    return problems


def push_problems(step: dict, job_if: object) -> list[str]:
    """A push may only target the owner's own GHCR namespace (inherits visibility)."""
    if gated(job_if, step.get("if")):
        return []
    uses = str(step.get("uses", ""))
    with_ = step.get("with") or {}
    problems = []
    if uses.startswith("docker/metadata-action"):
        for image in _lines(with_.get("images")):
            if not image.startswith(OWNER_NAMESPACE):
                problems.append(f"image '{image}' is outside {OWNER_NAMESPACE}")
    if uses.startswith("docker/build-push-action") and str(with_.get("push", "")).lower() == "true":
        for tag in _lines(with_.get("tags")):
            if tag.startswith("${{ steps.") and tag.endswith(".outputs.tags }}"):
                continue  # produced by docker/metadata-action, checked above
            if not tag.startswith(OWNER_NAMESPACE):
                problems.append(f"pushes tag '{tag}' outside {OWNER_NAMESPACE}")
    if uses.startswith("docker/login-action"):
        registry = str(with_.get("registry", ""))
        if registry and registry != "ghcr.io":
            problems.append(f"logs in to '{registry}', only ghcr.io is allowed")
    return problems


def _lines(value: object) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def source_violations(path: str, text: str) -> list[str]:
    """Scripts, engine code and the justfile have no gate: any hit fails."""
    problems = []
    for kind, pattern in PUBLIC_PATTERNS.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            problems.append(f"{path}:{line}: {kind}: {match.group(0)!r}")
    return problems


def scan_repo(root: Path = REPO) -> list[str]:
    problems: list[str] = []
    for workflow in sorted((root / ".github" / "workflows").glob("*.yml")):
        problems.extend(workflow_violations(workflow.name, workflow.read_text(encoding="utf-8")))
    sources = list((root / "scripts").rglob("*")) + list((root / "engine").rglob("*.py"))
    sources.append(root / "justfile")
    for path in sorted(sources):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        problems.extend(source_violations(str(path.relative_to(root)), text))
    return problems


# --------------------------------------------------------------------------- #
# the real repository
# --------------------------------------------------------------------------- #
def test_repository_makes_nothing_public():
    assert scan_repo() == []


def test_docs_deploy_requires_both_gates():
    data = yaml.safe_load((REPO / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8"))
    deploy = data["jobs"]["deploy"]
    assert gated(deploy.get("if")), deploy.get("if")


def test_owner_confirmation_variable_is_only_ever_read_in_conditions():
    pattern = re.compile(r"PUBLIC_RELEASE_CONFIRMED")
    for workflow in (REPO / ".github" / "workflows").glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for line in text.splitlines():
            if pattern.search(line) and line.lstrip().startswith("#"):
                continue
            if pattern.search(line):
                assert GATE_CONFIRMED in line, f"{workflow.name}: {line.strip()}"
    for tree in ("scripts", "engine"):
        for path in (REPO / tree).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".sh", ""} and "__pycache__" not in path.parts:
                assert "PUBLIC_RELEASE_CONFIRMED" not in path.read_text(encoding="utf-8", errors="ignore"), path


def test_release_and_toolbox_push_only_to_the_owner_namespace():
    for name in ("release.yml", "toolbox.yml", "app.yml", "infra.yml"):
        text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
        assert workflow_violations(name, text) == [], name
    release = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "D-053" in release  # the comment explaining why visibility is never touched
    # Comments are not in the parsed YAML; no executable part may mention visibility.
    assert "visibility" not in yaml.dump(yaml.safe_load(release)).lower()


# --------------------------------------------------------------------------- #
# fixtures: the detector itself must fail on the dangerous things
# --------------------------------------------------------------------------- #
UNGATED_PAGES = """
name: docs
on: {push: {branches: [main]}}
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/deploy-pages@v4
"""

PRIVATE_ONLY = UNGATED_PAGES.replace(
    "    runs-on: ubuntu-latest",
    "    if: ${{ !github.event.repository.private }}\n    runs-on: ubuntu-latest",
)

BOTH_GATES = UNGATED_PAGES.replace(
    "    runs-on: ubuntu-latest",
    "    if: ${{ !github.event.repository.private && vars.PUBLIC_RELEASE_CONFIRMED == 'true' }}\n"
    "    runs-on: ubuntu-latest",
)

CONFIRMED_ONLY_STEP = UNGATED_PAGES.replace(
    "      - uses: actions/deploy-pages@v4",
    "      - if: ${{ vars.PUBLIC_RELEASE_CONFIRMED == 'true' }}\n        uses: actions/deploy-pages@v4",
)


def test_ungated_deploy_pages_fails():
    assert workflow_violations("docs.yml", UNGATED_PAGES)


def test_deploy_pages_gated_on_only_one_condition_fails():
    assert workflow_violations("docs.yml", PRIVATE_ONLY)
    assert workflow_violations("docs.yml", CONFIRMED_ONLY_STEP)


def test_deploy_pages_gated_on_both_conditions_passes():
    assert workflow_violations("docs.yml", BOTH_GATES) == []


def test_step_level_gate_with_both_conditions_passes():
    step_gate = UNGATED_PAGES.replace(
        "      - uses: actions/deploy-pages@v4",
        "      - if: ${{ !github.event.repository.private && vars.PUBLIC_RELEASE_CONFIRMED == 'true' }}\n"
        "        uses: actions/deploy-pages@v4",
    )
    assert workflow_violations("docs.yml", step_gate) == []


@pytest.mark.parametrize(
    "command",
    [
        "gh repo edit owner/name --visibility public --accept-visibility-change-consequences",
        "gh repo edit owner/name --visibility=public",
        "gh api -X PATCH /user/packages/container/sealedstack/visibility -f visibility=public",
        "curl -X PATCH https://api.github.com/orgs/o/packages/container/x/visibility -d '{\"visibility\": \"public\"}'",
        "gh repo create owner/new --public",
        "brew tap-new owner/homebrew-tap",
        "git push https://example.com/owner/homebrew-tap.git",
        "npm publish --access public",
        "twine upload dist/*",
        "cargo publish",
    ],
)
def test_dangerous_run_steps_fail_unless_gated(command):
    workflow = f"""
name: x
on: workflow_dispatch
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - run: |
          {command}
"""
    assert workflow_violations("x.yml", workflow), command
    gated_workflow = workflow.replace(
        "    runs-on: ubuntu-latest",
        "    if: ${{ !github.event.repository.private && vars.PUBLIC_RELEASE_CONFIRMED == 'true' }}\n"
        "    runs-on: ubuntu-latest",
    )
    assert workflow_violations("x.yml", gated_workflow) == [], command


def test_yaml_visibility_public_in_with_fails():
    workflow = """
name: x
on: workflow_dispatch
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: some/action@v1
        with:
          visibility: public
"""
    assert workflow_violations("x.yml", workflow)


def test_push_outside_owner_namespace_fails():
    workflow = """
name: x
on: workflow_dispatch
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/login-action@v3
        with:
          registry: docker.io
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: |
            docker.io/someone/sealedstack:1
"""
    problems = workflow_violations("x.yml", workflow)
    assert any("docker.io" in p for p in problems)
    assert len(problems) == 2


def test_called_only_workflow_may_push_to_the_callers_configured_registry():
    workflow = """
name: x
on:
  workflow_call:
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: ${{ needs.setup.outputs.registry }}/app:1
"""
    assert workflow_violations("x.yml", workflow) == []
    # ...but a called workflow still may not make anything public.
    public = workflow + "      - run: gh repo edit o/n --visibility public\n"
    assert workflow_violations("x.yml", public)


def test_push_to_owner_namespace_passes():
    workflow = """
name: x
on: workflow_dispatch
jobs:
  j:
    runs-on: ubuntu-latest
    steps:
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository_owner }}/sealedstack
      - uses: docker/build-push-action@v6
        with:
          push: true
          tags: ${{ steps.meta.outputs.tags }}
"""
    assert workflow_violations("x.yml", workflow) == []


def test_source_files_have_no_gate_any_hit_fails():
    assert source_violations("scripts/x.sh", "gh repo edit o/n --visibility public\n")
    assert source_violations("engine/x.py", 'body = {"visibility": "public"}\n')
    assert source_violations("justfile", "release:\n    brew tap-new o/homebrew-tap\n")
    assert source_violations("engine/ok.py", "visibility_of_the_check = 'private only'\n") == []


def test_scan_repo_finds_a_planted_violation(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "engine").mkdir()
    (tmp_path / "justfile").write_text("default:\n    echo ok\n", encoding="utf-8")
    (tmp_path / ".github" / "workflows" / "docs.yml").write_text(UNGATED_PAGES, encoding="utf-8")
    (tmp_path / "scripts" / "publish.sh").write_text("gh repo edit o/n --visibility public\n", encoding="utf-8")
    problems = scan_repo(tmp_path)
    assert any("docs.yml" in p for p in problems)
    assert any("scripts/publish.sh" in p for p in problems)


def test_docs_workflow_is_only_called_or_dispatched_and_a_pull_request_never_deploys():
    # ci.yml calls docs.yml (D-082); it has no trigger of its own that a PR or a push could use.
    data = yaml.safe_load((REPO / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch", "workflow_call"}
    condition = str(data["jobs"]["deploy"]["if"])
    assert "github.event_name != 'pull_request'" in condition
    assert gated(condition)
    steps = data["jobs"]["build"]["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-pages-artifact"))
    assert "github.event_name != 'pull_request'" in str(upload.get("if"))
