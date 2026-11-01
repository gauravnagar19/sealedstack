"""What the infra repo's CI does for each event (UX §8, §12.1, D-052).

``infra.yml`` calls two entry points of the pinned engine image:

* ``ci resolve`` -- turn the GitHub event (and the optional dispatched command)
  into *one* engine command, or nothing, after checking who may run it; and
* ``ci exec`` -- run that command (behind the D-052 approval gate for writes) and
  report the result where the user looks: the plan comment on the PR, the
  checklist on the Setup PR, the Status issue, a result comment on the merged PR.

| Event                     | Command                                                  |
|---------------------------|----------------------------------------------------------|
| pull_request              | ``plan`` (``check`` on the Setup PR)                     |
| push to the default branch| ``apply`` (waits for ``/sealedstack approve`` if needed) |
| schedule                  | ``status``, then drift only if SEALEDSTACK_DRIFT_ENABLED |
| issue_comment             | the ``/sealedstack <cmd>`` in the comment               |
| workflow_dispatch         | the chosen command                                       |

Everything here is a pure function of an injected forge, so the whole mapping is
tested against the in-memory forge.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from . import approval
from . import checklist
from . import commands as commands_mod
from . import config as config_mod
from . import forge as forge_mod
from . import lock as lock_mod
from . import planreport
from . import setupjob
from . import statusissue

# Comment commands (`/sealedstack <cmd>`).
COMMENT_READ = ("check", "plan", "status", "drill", "drift", "verify")
COMMENT_WRITE = ("up", "apply")

# The second cron of the workflow template (weekly, Sunday 03:00 UTC) runs the restore drill for every app with a database.
DRILL_CRON = "0 3 * * 0"

# Commands that never touch a server or Dokploy, so the runner needs no tailnet.
NO_MESH = frozenset({"setup", "add-app", "app add", "reinstall"})

SETUP_BRANCH_PREFIX = "sealedstack/setup"
WAITING_MARKER = "<!-- sealedstack:waiting -->"
RESULT_MARKER = "<!-- sealedstack:result -->"

# A push that touches none of these cannot change what `apply` would do.
RELEVANT_PREFIXES = ("config/", "sealedstack.version", ".github/workflows/sealedstack.yml")


@dataclass
class Event:
    name: str
    action: str = ""
    actor: str = ""
    repo: str = ""
    ref: str = ""
    sha: str = ""
    default_branch: str = "main"
    payload: dict = field(default_factory=dict)
    input_command: str = ""
    input_args: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Event":
        payload: dict = {}
        path = env.get("GITHUB_EVENT_PATH", "")
        if path and Path(path).is_file():
            try:
                payload = json.loads(Path(path).read_text(encoding="utf-8") or "{}")
            except (OSError, json.JSONDecodeError):
                payload = {}
        default = (
            env.get("SEALEDSTACK_DEFAULT_BRANCH")
            or (payload.get("repository") or {}).get("default_branch")
            or "main"
        )
        return cls(
            name=env.get("GITHUB_EVENT_NAME", ""),
            action=str(payload.get("action") or ""),
            actor=env.get("GITHUB_ACTOR", ""),
            repo=env.get("GITHUB_REPOSITORY", ""),
            ref=env.get("GITHUB_REF", ""),
            sha=env.get("GITHUB_SHA", ""),
            default_branch=default,
            payload=payload,
            input_command=env.get("SEALEDSTACK_COMMAND", "").strip(),
            input_args=env.get("SEALEDSTACK_ARGS", "").strip(),
        )


@dataclass
class Resolution:
    command: str = ""  # the engine command to run; "" = nothing to run
    args: str = ""
    pr: int = 0
    note: str = ""  # why nothing runs, or what is about to
    checkout_ref: str = ""  # what the job must check out ("" = the event's default)
    follow: list = field(default_factory=list)  # extra commands after the first (schedule: drift)
    # True only when stack.yaml says `slots.secrets: ci`: the workflow then forwards the
    # repository secrets bundle to the engine, and only then (otherwise it reaches nobody).
    ci_secrets: bool = False

    @property
    def proceed(self) -> bool:
        return bool(self.command)

    @property
    def write(self) -> bool:
        return approval.requires_approval(self.command)

    @property
    def needs_mesh(self) -> bool:
        return self.proceed and self.command not in NO_MESH

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Resolution":
        """The resolution ``ci resolve`` handed over as job outputs (so ``exec`` does
        not resolve again, and a refusal is never posted twice)."""
        pr = env.get("SEALEDSTACK_RESOLVED_PR", "")
        return cls(
            command=env.get("SEALEDSTACK_RESOLVED_COMMAND", ""),
            args=env.get("SEALEDSTACK_RESOLVED_ARGS", ""),
            pr=int(pr) if pr.isdigit() else 0,
            checkout_ref=env.get("SEALEDSTACK_RESOLVED_CHECKOUT_REF", ""),
            follow=[c for c in env.get("SEALEDSTACK_RESOLVED_FOLLOW", "").split(",") if c],
        )

    def outputs(self) -> dict:
        return {
            "proceed": "true" if self.proceed else "false",
            "command": self.command,
            "args": " ".join(self.args.split()),
            "write": "true" if self.write else "false",
            "mesh": "true" if self.needs_mesh else "false",
            "pr": str(self.pr or ""),
            "checkout_ref": self.checkout_ref,
            "follow": ",".join(self.follow),
            "ci_secrets": "true" if self.ci_secrets else "false",
            "note": self.note.replace("\n", " "),
        }


def _skip(note: str) -> Resolution:
    return Resolution(note=note)


def _touches_relevant_files(payload: dict) -> Optional[bool]:
    """True/False from a push payload's file lists; None when it does not list them."""
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits:
        return None
    saw_files = False
    for commit in commits:
        for key in ("added", "modified", "removed"):
            files = commit.get(key)
            if files is None:
                return None
            for path in files:
                saw_files = True
                if str(path).startswith(RELEVANT_PREFIXES):
                    return True
    return False if saw_files or len(commits) < 20 else None


# Commands a workflow_dispatch may start: the comment-command set without `approve`
# (dispatching an approver's write command is itself the approval, D-052).
DISPATCH_COMMANDS = frozenset(commands_mod.COMMANDS) - {"approve"}


def _printable(text: str, limit: int = 40) -> str:
    """A command name safe to put in a log line or a comment."""
    clean = "".join(ch if ch.isprintable() and ch not in "`\n\r" else "?" for ch in str(text))
    return clean[:limit]


def drift_enabled(env: Mapping[str, str]) -> bool:
    """The scheduled drift switch (D-059): the repository variable ``SEALEDSTACK_DRIFT_ENABLED``."""
    return str(env.get("SEALEDSTACK_DRIFT_ENABLED", "")).strip().lower() == "true"


def resolve(
    event: Event,
    forge: forge_mod.Forge,
    config: Optional[config_mod.Config],
    *,
    drift: bool = False,
) -> Resolution:
    """Map an event to the command to run (or nothing), enforcing who may run it.

    ``drift`` is the scheduled-drift switch (``drift_enabled(env)``): the schedule
    always runs ``status`` and adds drift detection only when it is on. A manual
    ``drift`` (dispatch or comment) never depends on it.
    """
    approvers = config.approvers() if config else []
    name = event.name

    if name == "workflow_dispatch":
        command = event.input_command
        if not command:
            return _skip("workflow_dispatch without a command")
        if command not in DISPATCH_COMMANDS:
            # FR-4: the caller's choice list is not trusted to be the allow-list; the same
            # commands as the comment path (minus approve) are the only ones that run.
            return _skip(f"'{_printable(command)}' is not a sealedstack command (allowed: {', '.join(sorted(DISPATCH_COMMANDS))})")
        return Resolution(command=command, args=event.input_args, note=f"dispatched: {command}")

    if name == "pull_request":
        pr = event.payload.get("pull_request") or {}
        number = int(pr.get("number") or 0)
        if event.action not in ("opened", "synchronize", "reopened", "ready_for_review"):
            return _skip(f"pull_request action '{event.action}' needs no run")
        head_repo = ((pr.get("head") or {}).get("repo") or {}).get("full_name", "")
        if head_repo and event.repo and head_repo != event.repo:
            return _skip("pull request from a fork: secrets are not available; nothing is run")
        if config is None:
            return _skip("no config/ yet: run the setup command first")
        head_ref = str((pr.get("head") or {}).get("ref") or "")
        command = "check" if head_ref.startswith(SETUP_BRANCH_PREFIX) else "plan"
        return Resolution(command=command, pr=number, note=f"pull request #{number}: {command}")

    if name == "push":
        if event.ref != f"refs/heads/{event.default_branch}":
            return _skip(f"push to {event.ref or 'unknown ref'}: only the default branch applies")
        if config is None:
            return _skip("no config/ on the default branch yet: run the setup command first")
        if _touches_relevant_files(event.payload) is False:
            return _skip("this push changed no config, version pin or workflow: nothing to apply")
        return Resolution(command="apply", note="push to the default branch: apply")

    if name == "schedule":
        if config is None:
            return _skip("no config/ yet")
        if (event.payload or {}).get("schedule") == DRILL_CRON:
            if not (config.stack.get("drill") or {}).get("scheduled"):
                return _skip("the weekly restore drill is off (drill.scheduled in stack.yaml); run `drill` by hand when you want it")
            return Resolution(command="drill", args="all", note="scheduled: weekly restore drill of every managed database")
        if drift:
            return Resolution(command="status", follow=["drift"], note="scheduled: status, then drift detection")
        return Resolution(
            command="status",
            note="scheduled: status (scheduled drift is off; set the repository variable SEALEDSTACK_DRIFT_ENABLED=true to turn it on)",
        )

    if name == "issue_comment":
        return _resolve_comment(event, forge, config, approvers)

    return _skip(f"event '{name}' is not handled")


def _refuse(forge: forge_mod.Forge, event: Event, number: int, text: str) -> Resolution:
    if number:
        forge.upsert_comment(event.repo, number, text)
    return _skip(text)


def _resolve_comment(
    event: Event,
    forge: forge_mod.Forge,
    config: Optional[config_mod.Config],
    approvers: Sequence[str],
) -> Resolution:
    if event.action != "created":
        return _skip("only new comments are commands")
    comment = event.payload.get("comment") or {}
    issue = event.payload.get("issue") or {}
    number = int(issue.get("number") or 0)
    parsed = commands_mod.parse(str(comment.get("body") or ""))
    if parsed is None:
        return _skip("not a /sealedstack command")
    actor = str((comment.get("user") or {}).get("login") or event.actor)
    command, args = parsed.command, " ".join(parsed.args)
    is_pr = bool(issue.get("pull_request"))

    writer = commands_mod.authorize(forge, event.repo, actor)
    if not writer:
        return _refuse(
            forge, event, number,
            f"@{actor} thanks! Only users with write access can run `/sealedstack {command}`.",
        )

    if is_pr and command != "approve":
        # FR-3: `check`, `plan` and `status` on a PR check out its merge ref, with the job's
        # secrets in reach. A fork's tree (its config/, its sealedstack.version) must never
        # run with them, so nothing is run for a pull request from another repository.
        pr_info = forge.get_pull_request(event.repo, number)
        head_repo = str(pr_info.get("head_repo") or "")
        if not pr_info or head_repo != event.repo:
            return _refuse(
                forge, event, number,
                f"@{actor} `/sealedstack {command}` is not run on a pull request from a fork "
                f"({head_repo or 'unknown repository'}): the job would check out code from outside this repository "
                "while it holds your secrets. Copy the branch into this repository (a branch here, not a fork) and comment again.",
            )

    if command == "approve":
        if actor not in approvers:
            return _refuse(forge, event, number, f"@{actor} only the approvers listed in `stack.yaml: approvers` can approve.")
        if not is_pr:
            return _skip("approve applies to a pull request")
        pr = forge.get_pull_request(event.repo, number)
        if not pr.get("merged"):
            forge.upsert_comment(
                event.repo, number,
                f"@{actor} approval recorded. It applies to the plan above; merge this PR and the apply runs.",
            )
            return _skip("approval recorded; the pull request is not merged yet")
        head = forge.branch_head(event.repo, event.default_branch)
        if head and pr.get("merge_commit_sha") == head:
            forge.upsert_comment(event.repo, number, f"@{actor} approval recorded; applying the merged change now.")
            return Resolution(command="apply", pr=number, note=f"approved after merge (PR #{number}): apply")
        return _refuse(
            forge, event, number,
            f"@{actor} approval recorded, but this PR is no longer the tip of `{event.default_branch}`. "
            "The newest merged change needs its own approval.",
        )

    if command in COMMENT_READ:
        checkout = f"refs/pull/{number}/merge" if is_pr and command in ("check", "plan", "status") else ""
        return Resolution(command=command, args=args, pr=number if is_pr else 0, checkout_ref=checkout,
                          note=f"comment by {actor}: {command}")
    if command in COMMENT_WRITE:
        if actor not in approvers:
            return _refuse(forge, event, number, f"@{actor} only the approvers listed in `stack.yaml: approvers` can run `/sealedstack {command}`.")
        return Resolution(command=command, args=args, pr=number if is_pr else 0, note=f"comment by {actor}: {command}")
    return _refuse(
        forge, event, number,
        f"@{actor} `/sealedstack {command}` can only be started from Actions, Run workflow "
        "(it needs typed input or confirmation).",
    )


# --------------------------------------------------------------------------- #
# exec
# --------------------------------------------------------------------------- #
# A command runner executes `sealedstack <argv...>` and returns (rc, combined output).
CommandRunner = Callable[[Sequence[str]], "tuple[int, str]"]


def tee_runner(env: Mapping[str, str]) -> CommandRunner:
    """Run the engine as a child process, streaming its output while keeping it."""

    def run(argv: Sequence[str]) -> tuple[int, str]:
        proc = subprocess.Popen(
            [sys.executable or "python3", "-m", "sealedstack", *argv],
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        chunks: list[str] = []
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            chunks.append(line)
        return proc.wait(), "".join(chunks)

    return run


def _argv(command: str, args: str) -> list:
    return [*command.split(), *shlex.split(args or "")]


def _merged_pr(forge: forge_mod.Forge, event: Event) -> int:
    for pr in forge.pull_requests_for_commit(event.repo, event.sha) or []:
        if pr.get("merged") and pr.get("number"):
            return int(pr["number"])
    return 0


def execute(
    res: Resolution,
    event: Event,
    forge: forge_mod.Forge,
    config: Optional[config_mod.Config],
    run: CommandRunner,
    *,
    env: Optional[Mapping[str, str]] = None,
    status_fn: Optional[Callable[[], tuple]] = None,
    out=None,
    http=None,
    secrets=None,
    lock_fn: Optional[Callable[[], Optional[lock_mod.StateLock]]] = None,
) -> int:
    """Run the resolved command and report the result. Returns the job's exit code.

    ``http`` / ``secrets`` are injected by tests; in CI they are built from the
    config and the environment. ``lock_fn`` builds the remote state lock (D-062):
    write commands hold it for the whole run, reads never touch it.
    """
    out = out if out is not None else sys.stderr
    if not res.proceed:
        print(f"sealedstack ci: nothing to run ({res.note})", file=out)
        return 0
    approvers = config.approvers() if config else []
    pr = res.pr

    if res.write:
        decision = approval.check(
            forge, event.repo, res.command,
            commit_sha=event.sha, actor=event.actor, approvers=approvers,
            pr_number=pr or None, ref=event.ref, branch=event.default_branch,
        )
        if not decision.approved:
            target = decision.pr or pr or _merged_pr(forge, event)
            if decision.waiting and event.name == "push":
                text = (
                    f"{WAITING_MARKER}\n⏳ **Waiting for approval.** `{res.command}` for `{event.sha[:7]}` "
                    "runs once an approver comments `/sealedstack approve` on this pull request "
                    f"(approvers: {', '.join(approvers) or 'none configured'})."
                )
                if target:
                    forge.upsert_comment(event.repo, target, text, marker=WAITING_MARKER)
                print(f"sealedstack ci: waiting for /sealedstack approve — {decision.reason}", file=out)
                return 0
            print(f"sealedstack ci: refused — {decision.reason}", file=out)
            if target and event.name != "push":
                forge.upsert_comment(event.repo, target, f"❌ `{res.command}` refused: {decision.reason}")
            return 1
        pr = pr or decision.pr or 0

    held = None
    lock = lock_fn() if (res.write and lock_fn is not None) else None
    if res.write and lock is None:
        print("sealedstack ci: warning: no remote state lock (R2 credentials unavailable); "
              "break-glass runs cannot be excluded from this run", file=out)
    if lock is not None:
        try:
            held = lock.acquire()
        except lock_mod.LockUnavailable as exc:
            # First run: `up` creates the state bucket, so there is nothing to lock before it. Nothing else
            # can hold a lock that cannot exist yet; the next write command locks as usual.
            print(f"sealedstack ci: warning: no state lock yet ({exc}); the first `up` creates the state bucket. "
                  "Running without a lock.", file=out)
            lock = None
        except lock_mod.LockHeld as exc:
            print(f"sealedstack ci: refused — {exc}", file=out)
            target = pr or _merged_pr(forge, event)
            if target and event.name != "push":
                forge.upsert_comment(event.repo, target, f"❌ `{res.command}` refused: {exc}")
            return 1
        if held is not None:
            print(f"sealedstack ci: state lock taken (id {held['id']}, expires {held['expires']})", file=out)
    try:
        rc, output = _run_one(res.command, res.args, pr, event, forge, run, status_fn=status_fn, env=env, config=config,
                              http=http, secrets=secrets, lock_fn=lock_fn)
        for follow in res.follow:
            rc2, _ = _run_one(follow, "", pr, event, forge, run, status_fn=status_fn, env=env, config=config,
                              http=http, secrets=secrets, lock_fn=lock_fn)
            rc = rc or rc2
        return rc
    finally:
        if lock is not None and held is not None:
            try:
                if not lock.release(held):
                    print("sealedstack ci: warning: the state lock was no longer ours at the end of the run", file=out)
            except Exception as exc:  # noqa: BLE001 - never mask the command's own result
                print(f"sealedstack ci: warning: could not release the state lock ({exc}); it expires on its own", file=out)


def _run_one(
    command: str,
    args: str,
    pr: int,
    event: Event,
    forge: forge_mod.Forge,
    run: CommandRunner,
    *,
    status_fn: Optional[Callable[[], tuple]],
    env: Optional[Mapping[str, str]],
    config: Optional[config_mod.Config] = None,
    http=None,
    secrets=None,
    lock_fn: Optional[Callable[[], Optional[lock_mod.StateLock]]] = None,
) -> tuple[int, str]:
    if command == "status" and status_fn is not None:
        return _run_status(event, forge, status_fn, lock_fn)

    argv = _argv(command, args)
    setup_pr = command == "check" and bool(pr) and _is_setup_pr(forge, event.repo, pr)
    if setup_pr and config is not None:
        # The credentials may exist now: fill in what they reveal and import the
        # tailnet policy as commits on the setup branch before checking.
        try:
            setupjob.prepare_check(config.config_dir, env=env or {}, forge=forge, repo=event.repo, http=http, secrets=secrets)
        except Exception as exc:  # noqa: BLE001 - never block the checklist on discovery
            print(f"sealedstack ci: could not prepare the setup branch: {exc}", file=sys.stderr)
    if command == "check" and pr and "--json" not in argv:
        argv.append("--json")
    rc, output = run(argv)

    if command == "plan" and pr:
        forge.upsert_comment(event.repo, pr, planreport.render_plan(output, pr=pr, rc=rc), marker=approval.PLAN_MARKER)
    elif command == "check" and pr:
        results = _check_results(output)
        if results is not None:
            if setup_pr and config is not None:
                items = _setup_items(results, config, event, env or {})
            else:
                items = checklist.from_results(results)
            forge.upsert_comment(event.repo, pr, checklist.render(items), marker=checklist.MARKER)
    elif command in ("apply", "up") and pr:
        mark = f"{RESULT_MARKER}<!-- {command} {event.sha[:12]} -->"
        icon = "✅" if rc == 0 else "❌"
        verdict = "succeeded" if rc == 0 else f"failed (exit {rc})"
        tail = "\n".join(planreport.strip_ansi(output).strip().splitlines()[-30:]).replace("```", "'''")
        forge.upsert_comment(
            event.repo, pr,
            f"{mark}\n{icon} `{command}` {verdict} on `{event.sha[:7]}`.\n\n<details><summary>Last lines</summary>\n\n```\n{tail}\n```\n\n</details>",
            marker=mark,
        )
    return rc, output


def _is_setup_pr(forge: forge_mod.Forge, repo: str, number: int) -> bool:
    head = str((forge.get_pull_request(repo, number) or {}).get("head_ref") or "")
    return head.startswith(SETUP_BRANCH_PREFIX)


def _setup_items(results, config: config_mod.Config, event: Event, env: Mapping[str, str]):
    """The Setup checklist rows, from a fresh read of the (possibly just edited) config."""
    fresh = config_mod.load_config(config.config_dir, config.code_root, dict(env))
    version_file = Path(config.config_dir).parent / "sealedstack.version"
    version = version_file.read_text(encoding="utf-8").strip().lstrip("v") if version_file.is_file() else ""
    owner = event.repo.split("/")[0]
    return setupjob.checklist_items(results, fresh, repo=event.repo, owner=owner, version=version, env=env)


def _check_results(output: str):
    """The preflight results from a ``check --json`` run, or None if it is not JSON."""
    data = None
    decoder = json.JSONDecoder()
    offset = 0
    for line in output.splitlines(keepends=True):
        if line.startswith("{"):
            try:
                data, _ = decoder.raw_decode(output[offset:])
                break
            except json.JSONDecodeError:
                pass
        offset += len(line)
    if not isinstance(data, dict):
        return None

    class _R:
        def __init__(self, d):
            self.id, self.status = d.get("id", ""), d.get("status", "fail")
            self.detail, self.fix = d.get("detail", ""), d.get("fix", "")

    return [_R(r) for r in data.get("results", [])]


STALE_LOCK_MARKER = "<!-- sealedstack:stale-lock -->"


def _run_status(event: Event, forge: forge_mod.Forge, status_fn: Callable[[], tuple],
                lock_fn: Optional[Callable[[], Optional[lock_mod.StateLock]]] = None) -> tuple[int, str]:
    """Compute health, update the pinned Status issue, comment on every problem."""
    config, version, health = status_fn()
    body = statusissue.render_from_health(config, version, health)
    statusissue.publish(forge, event.repo, body)
    problems = [r for r in health if r.status == "fail"]
    for result in problems:
        statusissue.post_problem(forge, event.repo, f"🔴 {result.id}: {result.detail}" + (f"\nFix: {result.fix}" if result.fix else ""))
    if lock_fn is not None:  # D-062: a lock held longer than an hour is a problem (a read, never a lock)
        try:
            lock = lock_fn()
            report = lock.stale_report() if lock is not None else ""
            if report:
                statusissue.post_problem(forge, event.repo, f"🔴 state lock: {report}", marker=STALE_LOCK_MARKER)
        except Exception as exc:  # noqa: BLE001 - the lock check must never fail the status run
            print(f"sealedstack ci: could not read the state lock ({exc})", file=sys.stderr)
    return 0, body


def write_outputs(path: str, outputs: Mapping[str, str]) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in outputs.items():
            handle.write(f"{key}={value}\n")


# --------------------------------------------------------------------------- #
# CLI entry points: `sealedstack ci resolve|exec`
# --------------------------------------------------------------------------- #
def ci_secrets_enabled(config: Optional[config_mod.Config]) -> bool:
    """Does this infra repo read its secrets from the CI platform (`slots.secrets: ci`)?

    Anything else (bitwarden, the default; sops; a CLI backend; env; no config at all) is
    False, so the workflow never hands the repository secrets to the engine for it.
    """
    if config is None:
        return False
    slots = (config.stack or {}).get("slots") or {}
    return str(slots.get("secrets") or "").strip().lower() == "ci"


def _load_config(env: Mapping[str, str], code_root: Path) -> Optional[config_mod.Config]:
    config_dir = config_mod.resolve_config_dir(None, env, code_root)
    if not (Path(config_dir) / "stack.yaml").is_file():
        return None
    return config_mod.load_config(config_dir, code_root, env)


def _default_lock_fn(config: Optional[config_mod.Config], env: Mapping[str, str]):
    """Build the R2 state lock lazily: only write commands and status need it."""
    def build():
        if config is None:
            return None
        from . import cli

        return cli._state_lock(config, dict(env), kind="ci")

    return build


def main(argv: Optional[list] = None, *, env: Optional[Mapping[str, str]] = None, forge: Optional[forge_mod.Forge] = None,
         runner: Optional[CommandRunner] = None, status_fn=None, lock_fn=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    env = dict(os.environ if env is None else env)
    verb = args[0] if args else ""
    if verb not in ("resolve", "exec"):
        print("usage: sealedstack ci <resolve|exec> [--output FILE]", file=sys.stderr)
        return 2
    output_file = ""
    if "--output" in args:
        output_file = args[args.index("--output") + 1]
    code_root = config_mod.find_code_root(env)
    event = Event.from_env(env)
    config = _load_config(env, code_root)
    if forge is None:
        try:
            forge = forge_mod.GitHubForge.from_env(env)
        except forge_mod.ForgeError as exc:
            print(f"sealedstack ci: {exc}", file=sys.stderr)
            return 1

    if verb == "resolve":
        res = resolve(event, forge, config, drift=drift_enabled(env))
        res.ci_secrets = ci_secrets_enabled(config)
        print(f"sealedstack ci: {res.note or 'nothing to do'}", file=sys.stderr)
        if output_file:
            write_outputs(output_file, res.outputs())
        else:
            print(json.dumps(res.outputs(), indent=2))
        return 0

    # exec: run what `resolve` decided (handed over as SEALEDSTACK_RESOLVED_*).
    res = Resolution.from_env(env)
    if status_fn is None:
        status_fn = _default_status_fn(env, code_root)
    return execute(res, event, forge, config, runner or tee_runner(env), env=env, status_fn=status_fn,
                   lock_fn=lock_fn if lock_fn is not None else _default_lock_fn(config, env))


def _default_status_fn(env: Mapping[str, str], code_root: Path):
    def run():
        from . import preflight

        config = config_mod.load_config(config_mod.resolve_config_dir(None, env, code_root), code_root, env)
        version = config_mod.read_version(code_root)
        ctx = preflight.build_context(config, env=dict(env), version=version)
        return config, version, preflight.run_checks(ctx)

    return run


if __name__ == "__main__":
    raise SystemExit(main())
