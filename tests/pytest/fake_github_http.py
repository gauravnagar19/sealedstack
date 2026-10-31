"""A paged fake of the GitHub REST endpoints the forge lists (no network).

It behaves like the real API where it matters for DR-7: list endpoints return at
most ``per_page`` items (30 when the client does not say), a ``Link`` header names
the next page, search is a separate endpoint that can be empty or fail, and
pull requests appear in the issues list. Every request is recorded.
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from sealedstack.http import HttpClient, Response

API = "https://api.github.com"


class PagedGitHub(HttpClient):
    def __init__(self, repo="you/my-infra", *, page_size=None):
        self.repo = repo
        self.issues = []        # dicts: number, title, body, pull_request?
        self.comments = {}      # issue number -> list of dicts (oldest first)
        self.variables = {}
        self.requests = []      # (method, path with query)
        self.search_status = 200
        self.search_results = None   # None = really search the bodies
        self.page_size = page_size   # force a small page size (to need several pages)
        self._next = 1000

    def _id(self):
        self._next += 1
        return self._next

    # -- seed helpers -------------------------------------------------------------
    def add_issue(self, body="", *, title="issue", pr=False):
        number = self._id()
        issue = {"number": number, "title": title, "body": body}
        if pr:
            issue["pull_request"] = {"url": "x"}
        self.issues.append(issue)
        return number

    def add_comments(self, number, count, *, body="chatter {i}", author="someone"):
        rows = self.comments.setdefault(number, [])
        for i in range(count):
            n = len(rows)
            rows.append({"id": self._id(), "body": body.format(i=n), "user": {"login": author},
                         "created_at": f"2026-10-30T00:{n // 60:02d}:{n % 60:02d}Z", "updated_at": f"2026-10-30T00:{n // 60:02d}:{n % 60:02d}Z"})
        return rows

    # -- the API ---------------------------------------------------------------------
    def _reply(self, status, body=None, headers=None):
        return Response(status, headers or {}, json.dumps(body if body is not None else {}).encode(), "")

    def _page(self, path, rows, query):
        per_page = min(int(query.get("per_page", [30])[0]), 100)
        if self.page_size:
            per_page = min(per_page, self.page_size)
        page = int(query.get("page", [1])[0])
        chunk = rows[(page - 1) * per_page: page * per_page]
        headers = {}
        if page * per_page < len(rows):
            q = {k: v[0] for k, v in query.items() if k != "page"}
            q["page"] = page + 1
            headers["Link"] = f'<{API}{path}?' + "&".join(f"{k}={v}" for k, v in q.items()) + '>; rel="next"'
        return self._reply(200, chunk, headers)

    def request(self, method, url, *, headers=None, json_body=None, form_body=None, timeout=30.0):
        assert url.startswith(API), f"left the API host: {url}"
        parsed = urlparse(url)
        path, query = parsed.path, parse_qs(parsed.query)
        self.requests.append((method, path + ("?" + parsed.query if parsed.query else "")))
        repo = self.repo

        if path == f"/repos/{repo}/issues" and method == "GET":
            return self._page(path, list(reversed(self.issues)), query)  # newest first, PRs included
        if path == f"/repos/{repo}/issues" and method == "POST":
            number = self.add_issue(json_body.get("body", ""), title=json_body.get("title", ""))
            return self._reply(201, {"number": number})
        m = re.fullmatch(rf"/repos/{repo}/issues/(\d+)", path)
        if m and method in ("GET", "PATCH"):
            found = next((i for i in self.issues if i["number"] == int(m.group(1))), None)
            if found and method == "PATCH":
                found.update({k: v for k, v in json_body.items() if k in ("title", "body")})
            return self._reply(200, found) if found else self._reply(404)
        m = re.fullmatch(rf"/repos/{repo}/issues/(\d+)/comments", path)
        if m:
            number = int(m.group(1))
            if method == "GET":
                return self._page(path, self.comments.get(number, []), query)
            if method == "POST":
                row = self.add_comments(number, 1, body=json_body["body"])[-1]
                return self._reply(201, row)
        m = re.fullmatch(rf"/repos/{repo}/issues/comments/(\d+)", path)
        if m and method == "PATCH":
            for rows in self.comments.values():
                for row in rows:
                    if row["id"] == int(m.group(1)):
                        row["body"] = json_body["body"]
                        return self._reply(200, row)
            return self._reply(404)
        if path == "/search/issues":
            if self.search_status != 200:
                return self._reply(self.search_status, {"message": "rate limit"})
            if self.search_results is not None:
                return self._reply(200, {"items": self.search_results})
            term = unquote(query["q"][0]).split('"')[1]
            return self._reply(200, {"items": [i for i in self.issues if term in i["body"]]})
        m = re.fullmatch(rf"/repos/{repo}/actions/variables/(\w+)", path)
        if m and method == "GET":
            name = m.group(1)
            return self._reply(200, {"name": name, "value": self.variables[name]}) if name in self.variables else self._reply(404)
        if path == f"/repos/{repo}/actions/variables" and method == "POST":
            self.variables[json_body["name"]] = json_body["value"]
            return self._reply(201)
        m = re.fullmatch(rf"/repos/{repo}/actions/variables/(\w+)", path)
        if m and method == "PATCH":
            self.variables[m.group(1)] = json_body["value"]
            return self._reply(204)
        return self._reply(404, {"message": f"fake: {method} {path}"})

    def listed(self, fragment):
        return [r for r in self.requests if fragment in r[1]]
