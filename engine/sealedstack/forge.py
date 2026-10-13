"""Forge adapters (spec §5, M7).

The `forge` slot abstracts the hosting platform the infra repo lives on: read an
app-repo file at a ref, open a PR/MR, comment on/update a PR or issue, set/read
CI variables and secrets on an app repo, and create/update the pinned Status
issue. GitHub is the reference implementation; ``InMemoryForge`` is the mocked
forge used by the M7 acceptance tests (no network, no real org).

The engine only ever *reads* secrets (D-038); the one write here is publishing a
value the infra repo already owns into an app repo's CI (D-023), never back to a
secret source.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from .http import HttpClient, UrllibClient


def repo_from_git(code_root) -> str:
    """Derive ``owner/name`` from the origin remote, or ""."""
    try:
        url = subprocess.run(
            ["git", "-C", str(code_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    return match.group(1) if match else ""


class ForgeError(RuntimeError):
    pass


class Forge:
    """The forge contract (spec §5)."""

    def read_file(self, repo: str, path: str, ref: str = "main") -> str:  # pragma: no cover
        raise NotImplementedError

    def list_files(self, repo: str, path: str, ref: str = "main") -> list:  # pragma: no cover
        """File paths directly under ``path`` (empty when the directory is absent)."""
        raise NotImplementedError

    def open_pr(
        self,
        repo: str,
        *,
        branch: str,
        title: str,
        body: str,
        files: Mapping[str, str],
        base: str = "main",
        deletes: Sequence[str] = (),
    ) -> int:  # pragma: no cover
        """Open a PR that writes ``files`` (path -> text) and deletes ``deletes``."""
        raise NotImplementedError

    def upsert_comment(self, repo: str, number: int, body: str, *, marker: str = "") -> int:  # pragma: no cover
        raise NotImplementedError

    def issue_comments(self, repo: str, number: int) -> list:  # pragma: no cover
        """Comments on an issue/PR, oldest first:
        ``[{author, body, created, updated}, ...]`` (``created``/``updated`` sort
        chronologically: ISO-8601 strings on GitHub, integers in memory)."""
        raise NotImplementedError

    def pull_requests_for_commit(self, repo: str, sha: str) -> list:  # pragma: no cover
        """PRs that contain a commit: ``[{number, merged}, ...]``."""
        raise NotImplementedError

    def set_variable(self, repo: str, name: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def get_variable(self, repo: str, name: str) -> str:  # pragma: no cover
        raise NotImplementedError

    def set_secret(self, repo: str, name: str, value: str, *, environment: str = "") -> None:  # pragma: no cover
        raise NotImplementedError

    def get_issue(self, repo: str, marker: str) -> Optional[int]:  # pragma: no cover
        raise NotImplementedError

    def create_issue(self, repo: str, *, title: str, body: str, pinned: bool = False) -> int:  # pragma: no cover
        raise NotImplementedError

    def update_issue(self, repo: str, number: int, *, title: Optional[str] = None, body: Optional[str] = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def ensure_environment(self, repo: str, environment: str) -> None:  # pragma: no cover
        """Create the environment (so a required reviewer can gate releases)."""
        raise NotImplementedError

    def permission(self, repo: str, user: str) -> str:  # pragma: no cover
        """One of admin|write|read|none (used to gate comment commands)."""
        raise NotImplementedError

    def branch_head(self, repo: str, branch: str) -> str:  # pragma: no cover
        """The commit SHA at the tip of ``branch`` (empty if unknown)."""
        raise NotImplementedError

    def get_pull_request(self, repo: str, number: int) -> dict:  # pragma: no cover
        """``{number, state, merged, merge_commit_sha, head_ref, head_repo, base_ref, title}``
        (empty dict when the PR does not exist)."""
        raise NotImplementedError

    def find_open_pr(self, repo: str, branch: str) -> int:  # pragma: no cover
        """The number of the open PR whose head is ``branch`` (0 when there is none)."""
        raise NotImplementedError

    def commit_files(self, repo: str, branch: str, files: Mapping[str, str], message: str) -> None:  # pragma: no cover
        """Commit ``files`` (path -> text) to an existing ``branch`` (one commit per file on GitHub)."""
        raise NotImplementedError

    def latest_successful_deployment(self, repo: str, environment: str = "production") -> dict:  # pragma: no cover
        """The newest deployment of ``environment`` whose outcome is success (D-060):
        ``{id, ref, version, servers}``; empty dict when there is none."""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Released versions are recorded as deployments in the app repo (D-060)
# --------------------------------------------------------------------------- #
_OUTCOME_STATES = ("success", "failure", "error")


def deployment_outcome(statuses: Sequence[Mapping]) -> str:
    """The outcome of one deployment from its statuses (newest first).

    Only ``success``, ``failure`` and ``error`` decide the outcome: ``queued``,
    ``in_progress``, ``pending`` and ``inactive`` do not. GitHub marks an older
    successful deployment ``inactive`` when a newer one succeeds, so the newest
    deciding status is the one that counts. ``""`` means no outcome yet.
    """
    for status in statuses:
        state = str(status.get("state") or "")
        if state in _OUTCOME_STATES:
            return state
    return ""


def deployment_record(deployment: Mapping) -> dict:
    """``{id, ref, version, servers}`` from a GitHub-shaped deployment (payload may be a string)."""
    payload = deployment.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}
    ref = str(deployment.get("ref") or "")
    servers = payload.get("servers")
    return {
        "id": deployment.get("id"),
        "ref": ref,
        "version": str(payload.get("version") or ref),
        "servers": [str(s) for s in servers] if isinstance(servers, list) else [],
    }


# --------------------------------------------------------------------------- #
# GitHub (reference)
# --------------------------------------------------------------------------- #
class GitHubForge(Forge):
    """GitHub REST via a user-owned GitHub App installation token."""

    api = "https://api.github.com"

    def __init__(self, token: str, *, http: Optional[HttpClient] = None, seal=None):
        self.token = token
        self.http = http or UrllibClient()
        self._seal = seal if seal is not None else libsodium_sealer()

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, *, http: Optional[HttpClient] = None) -> "GitHubForge":
        import os

        env = dict(os.environ if env is None else env)
        token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN") or ""
        if not token:
            raise ForgeError("GH_TOKEN is not set")
        return cls(token, http=http)

    def _request(self, method: str, path: str, *, json_body=None, text: bool = False) -> object:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if text:
            headers["Accept"] = "application/vnd.github.raw"
        response = self.http.request(method, f"{self.api}{path}", headers=headers, json_body=json_body)
        if response.status >= 400:
            raise ForgeError(f"{method} {path} failed (HTTP {response.status})")
        if text:
            return response.text
        if not response.body:
            return None
        return response.json()

    def read_file(self, repo: str, path: str, ref: str = "main") -> str:
        return str(self._request("GET", f"/repos/{repo}/contents/{path}?ref={ref}", text=True))

    def list_files(self, repo: str, path: str, ref: str = "main") -> list:
        try:
            data = self._request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        except ForgeError:
            return []
        if not isinstance(data, list):
            return []
        return [str(item.get("path")) for item in data if item.get("type") == "file"]

    def _file_sha(self, repo: str, path: str, ref: str) -> Optional[str]:
        """The blob sha of an existing file on ``ref`` (None when it does not exist)."""
        try:
            data = self._request("GET", f"/repos/{repo}/contents/{path}?ref={ref}")
        except ForgeError:
            return None
        if not isinstance(data, dict):
            return None
        return str(data.get("sha") or "") or None

    def open_pr(
        self,
        repo: str,
        *,
        branch: str,
        title: str,
        body: str,
        files: Mapping[str, str],
        base: str = "main",
        deletes: Sequence[str] = (),
    ) -> int:
        base_sha = self._request("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
        self._request("POST", f"/repos/{repo}/git/refs", json_body={"ref": f"refs/heads/{branch}", "sha": base_sha})
        for path, content in files.items():
            payload = {"message": f"sealedstack: {title}", "content": _b64(content), "branch": branch}
            existing = self._file_sha(repo, path, branch)
            if existing:
                payload["sha"] = existing  # GitHub requires the blob sha to update a file
            self._request("PUT", f"/repos/{repo}/contents/{path}", json_body=payload)
        for path in deletes:
            existing = self._file_sha(repo, path, branch)
            if not existing:
                continue  # already gone
            self._request(
                "DELETE",
                f"/repos/{repo}/contents/{path}",
                json_body={"message": f"sealedstack: {title}", "sha": existing, "branch": branch},
            )
        pr = self._request("POST", f"/repos/{repo}/pulls", json_body={"title": title, "body": body, "head": branch, "base": base})
        return int(pr["number"])

    def upsert_comment(self, repo: str, number: int, body: str, *, marker: str = "") -> int:
        if marker:
            comments = self._request("GET", f"/repos/{repo}/issues/{number}/comments") or []
            for comment in comments:
                if marker in (comment.get("body") or ""):
                    # An unchanged body is left alone so its edit time (and with it
                    # the approvals that came after it, D-052) is not disturbed.
                    if (comment.get("body") or "") != body:
                        self._request("PATCH", f"/repos/{repo}/issues/comments/{comment['id']}", json_body={"body": body})
                    return int(comment["id"])
        created = self._request("POST", f"/repos/{repo}/issues/{number}/comments", json_body={"body": body})
        return int(created["id"])

    def issue_comments(self, repo: str, number: int) -> list:
        comments = self._request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100") or []
        return [
            {
                "author": (c.get("user") or {}).get("login", ""),
                "body": c.get("body") or "",
                "created": c.get("created_at") or "",
                "updated": c.get("updated_at") or c.get("created_at") or "",
            }
            for c in comments
        ]

    def pull_requests_for_commit(self, repo: str, sha: str) -> list:
        prs = self._request("GET", f"/repos/{repo}/commits/{sha}/pulls") or []
        return [
            {"number": int(pr.get("number", 0)), "merged": bool(pr.get("merged_at"))}
            for pr in prs
        ]

    def branch_head(self, repo: str, branch: str) -> str:
        data = self._request("GET", f"/repos/{repo}/branches/{branch}") or {}
        return str(((data.get("commit") or {}).get("sha")) or "")

    def get_pull_request(self, repo: str, number: int) -> dict:
        try:
            pr = self._request("GET", f"/repos/{repo}/pulls/{number}") or {}
        except ForgeError:
            return {}
        head = pr.get("head") or {}
        return {
            "number": int(pr.get("number") or number),
            "state": str(pr.get("state") or ""),
            "merged": bool(pr.get("merged")),
            "merge_commit_sha": str(pr.get("merge_commit_sha") or ""),
            "head_ref": str(head.get("ref") or ""),
            "head_repo": str((head.get("repo") or {}).get("full_name") or ""),
            "base_ref": str((pr.get("base") or {}).get("ref") or ""),
            "title": str(pr.get("title") or ""),
        }

    def find_open_pr(self, repo: str, branch: str) -> int:
        owner = repo.split("/")[0]
        prs = self._request("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}") or []
        return int(prs[0]["number"]) if prs else 0

    def commit_files(self, repo: str, branch: str, files: Mapping[str, str], message: str) -> None:
        for path, content in files.items():
            payload = {"message": message, "content": _b64(content), "branch": branch}
            existing = self._file_sha(repo, path, branch)
            if existing:
                payload["sha"] = existing
            self._request("PUT", f"/repos/{repo}/contents/{path}", json_body=payload)

    def set_variable(self, repo: str, name: str, value: str) -> None:
        try:
            self._request("POST", f"/repos/{repo}/actions/variables", json_body={"name": name, "value": value})
        except ForgeError:
            self._request("PATCH", f"/repos/{repo}/actions/variables/{name}", json_body={"name": name, "value": value})

    def get_variable(self, repo: str, name: str) -> str:
        data = self._request("GET", f"/repos/{repo}/actions/variables/{name}")
        return str((data or {}).get("value") or "")

    def set_secret(self, repo: str, name: str, value: str, *, environment: str = "") -> None:
        # GitHub requires a libsodium sealed box; inject a sealer (e.g. pynacl) or
        # fall back to `gh secret set`, which encrypts for you.
        if self._seal is None:
            raise ForgeError("set_secret needs a libsodium sealer (or use `gh secret set`)")
        base = f"/repos/{repo}/actions/secrets" if not environment else f"/repos/{repo}/environments/{environment}/secrets"
        key = self._request("GET", f"{base}/public-key")
        sealed = self._seal(key["key"], value)
        self._request("PUT", f"{base}/{name}", json_body={"encrypted_value": sealed, "key_id": key["key_id"]})

    def get_issue(self, repo: str, marker: str) -> Optional[int]:
        issues = self._request("GET", f"/repos/{repo}/issues?state=all&per_page=100") or []
        for issue in issues:
            if "pull_request" in issue:
                continue
            if marker in (issue.get("body") or ""):
                return int(issue["number"])
        return None

    def create_issue(self, repo: str, *, title: str, body: str, pinned: bool = False) -> int:
        issue = self._request("POST", f"/repos/{repo}/issues", json_body={"title": title, "body": body})
        return int(issue["number"])

    def update_issue(self, repo: str, number: int, *, title: Optional[str] = None, body: Optional[str] = None) -> None:
        payload = {}
        if title is not None:
            payload["title"] = title
        if body is not None:
            payload["body"] = body
        self._request("PATCH", f"/repos/{repo}/issues/{number}", json_body=payload)

    def ensure_environment(self, repo: str, environment: str) -> None:
        try:
            self._request("PUT", f"/repos/{repo}/environments/{environment}")
        except ForgeError:
            pass

    def permission(self, repo: str, user: str) -> str:
        try:
            data = self._request("GET", f"/repos/{repo}/collaborators/{user}/permission")
            return str((data or {}).get("permission") or "none")
        except ForgeError:
            return "none"

    def latest_successful_deployment(self, repo: str, environment: str = "production") -> dict:
        deployments = self._request("GET", f"/repos/{repo}/deployments?environment={environment}&per_page=30") or []
        for deployment in deployments:  # newest first
            statuses = self._request("GET", f"/repos/{repo}/deployments/{deployment['id']}/statuses?per_page=30") or []
            if deployment_outcome(statuses) == "success":
                return deployment_record(deployment)
        return {}


def _b64(text: str) -> str:
    import base64

    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def libsodium_sealer():
    """A GitHub-secret sealer using libsodium sealed boxes (needs ``pynacl``).

    Returns ``None`` when ``pynacl`` is unavailable, so callers can fall back to
    another path (e.g. ``gh secret set``)."""
    try:
        from nacl import encoding, public  # type: ignore
    except Exception:  # noqa: BLE001
        return None

    def seal(key_b64: str, value: str) -> str:
        pk = public.PublicKey(key_b64.encode("ascii"), encoding.Base64Encoder)
        box = public.SealedBox(pk)
        return encoding.Base64Encoder.encode(box.encrypt(value.encode("utf-8"))).decode("ascii")

    return seal


# --------------------------------------------------------------------------- #
# In-memory (tests) — the "mocked forge"
# --------------------------------------------------------------------------- #
@dataclass
class _Comment:
    id: int
    body: str
    author: str = ""
    created: int = 0
    updated: int = 0


@dataclass
class InMemoryForge(Forge):
    """A forge that records everything in memory; used by the M7 acceptance."""

    files: dict = field(default_factory=dict)
    prs: list = field(default_factory=list)
    comments: dict = field(default_factory=dict)
    variables: dict = field(default_factory=dict)
    secrets: dict = field(default_factory=dict)
    issues: dict = field(default_factory=dict)
    permissions: dict = field(default_factory=dict)
    environments: set = field(default_factory=set)
    commit_prs: dict = field(default_factory=dict)
    branch_heads: dict = field(default_factory=dict)
    pull_requests: dict = field(default_factory=dict)
    commits: list = field(default_factory=list)
    deployments: dict = field(default_factory=dict)
    _next: int = 1000
    _clock: int = 0

    def _id(self) -> int:
        self._next += 1
        return self._next

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def add_comment(self, repo: str, number: int, author: str, body: str) -> int:
        """Record a comment with a known author (for approval tests)."""
        now = self._tick()
        comment = _Comment(self._id(), body, author, now, now)
        self.comments.setdefault((repo, number), []).append(comment)
        return comment.id

    def issue_comments(self, repo: str, number: int) -> list:
        return [
            {"author": c.author, "body": c.body, "created": c.created, "updated": c.updated}
            for c in self.comments.get((repo, number), [])
        ]

    def get_pull_request(self, repo: str, number: int) -> dict:
        return dict(self.pull_requests.get((repo, number), {}))

    def find_open_pr(self, repo: str, branch: str) -> int:
        for pr in self.prs:
            if pr["repo"] == repo and pr["branch"] == branch and not pr.get("closed"):
                return int(pr["number"])
        return 0

    def commit_files(self, repo: str, branch: str, files: Mapping[str, str], message: str) -> None:
        for path, content in files.items():
            self.files[(repo, branch, path)] = content
        self.commits.append({"repo": repo, "branch": branch, "message": message, "files": sorted(files)})

    def pull_requests_for_commit(self, repo: str, sha: str) -> list:
        return list(self.commit_prs.get((repo, sha), []))

    def read_file(self, repo: str, path: str, ref: str = "main") -> str:
        return self.files.get((repo, ref, path), "")

    def list_files(self, repo: str, path: str, ref: str = "main") -> list:
        prefix = path.rstrip("/") + "/"
        return sorted(
            p
            for (r, rf, p) in self.files
            if r == repo and rf == ref and p.startswith(prefix) and "/" not in p[len(prefix) :]
        )

    def open_pr(
        self,
        repo: str,
        *,
        branch: str,
        title: str,
        body: str,
        files: Mapping[str, str],
        base: str = "main",
        deletes: Sequence[str] = (),
    ) -> int:
        number = self._id()
        for path, content in files.items():
            self.files[(repo, branch, path)] = content
        for path in deletes:
            self.files.pop((repo, branch, path), None)
        self.prs.append(
            {
                "number": number,
                "repo": repo,
                "branch": branch,
                "title": title,
                "body": body,
                "files": dict(files),
                "deletes": list(deletes),
                "base": base,
            }
        )
        self.pull_requests[(repo, number)] = {
            "number": number, "state": "open", "merged": False, "merge_commit_sha": "",
            "head_ref": branch, "head_repo": repo, "base_ref": base, "title": title,
        }
        return number

    def upsert_comment(self, repo: str, number: int, body: str, *, marker: str = "") -> int:
        bucket = self.comments.setdefault((repo, number), [])
        if marker:
            for comment in bucket:
                if marker in comment.body:
                    if comment.body != body:
                        comment.body = body
                        comment.updated = self._tick()
                    return comment.id
        now = self._tick()
        comment = _Comment(self._id(), body, "", now, now)
        bucket.append(comment)
        return comment.id

    def set_variable(self, repo: str, name: str, value: str) -> None:
        self.variables[(repo, name)] = value

    def get_variable(self, repo: str, name: str) -> str:
        return self.variables.get((repo, name), "")

    def set_secret(self, repo: str, name: str, value: str, *, environment: str = "") -> None:
        self.secrets[(repo, environment, name)] = value

    def get_issue(self, repo: str, marker: str) -> Optional[int]:
        for number, issue in self.issues.items():
            if issue["repo"] == repo and marker in issue["body"]:
                return number
        return None

    def create_issue(self, repo: str, *, title: str, body: str, pinned: bool = False) -> int:
        number = self._id()
        self.issues[number] = {"repo": repo, "number": number, "title": title, "body": body, "pinned": pinned}
        return number

    def update_issue(self, repo: str, number: int, *, title: Optional[str] = None, body: Optional[str] = None) -> None:
        issue = self.issues[number]
        if title is not None:
            issue["title"] = title
        if body is not None:
            issue["body"] = body

    def ensure_environment(self, repo: str, environment: str) -> None:
        self.environments.add((repo, environment))

    def permission(self, repo: str, user: str) -> str:
        return self.permissions.get((repo, user), "none")

    def branch_head(self, repo: str, branch: str) -> str:
        return str(self.branch_heads.get((repo, branch), ""))

    def add_deployment(
        self,
        repo: str,
        *,
        ref: str,
        statuses: Sequence[str] = ("success",),
        environment: str = "production",
        payload: Optional[Mapping] = None,
    ) -> int:
        """Record a deployment (tests). ``statuses`` is in the order they were posted."""
        number = self._id()
        self.deployments.setdefault(repo, []).append(
            {
                "id": number,
                "ref": ref,
                "environment": environment,
                "payload": dict(payload) if payload is not None else {"version": ref, "servers": []},
                "statuses": [{"state": state} for state in statuses],
            }
        )
        return number

    def latest_successful_deployment(self, repo: str, environment: str = "production") -> dict:
        rows = [d for d in self.deployments.get(repo, []) if d["environment"] == environment]
        for deployment in reversed(rows):  # newest first, like GitHub
            if deployment_outcome(list(reversed(deployment["statuses"]))) == "success":
                return deployment_record(deployment)
        return {}
