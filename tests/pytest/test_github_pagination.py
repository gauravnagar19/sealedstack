"""DR-7: the GitHub forge reads every page, and the Status issue is found reliably."""
from __future__ import annotations

import pytest

from fake_github_http import API, PagedGitHub
from sealedstack import approval, forge, statusissue

REPO = "you/my-infra"


def gh(http):
    return forge.GitHubForge("github_pat_x", http=http, seal=None)


# -- the Link header -------------------------------------------------------------------
def test_next_link_is_parsed_case_insensitively():
    link = '<https://api.github.com/x?page=2>; rel="next", <https://api.github.com/x?page=9>; rel="last"'
    assert forge.next_link({"Link": link}) == "https://api.github.com/x?page=2"
    assert forge.next_link({"link": link}) == "https://api.github.com/x?page=2"
    assert forge.next_link({"Link": '<https://api.github.com/x?page=1>; rel="prev"'}) == ""
    assert forge.next_link({}) == "" and forge.next_link(None) == ""


def test_a_link_to_another_host_is_never_followed_with_the_token():
    class Evil(PagedGitHub):
        def request(self, method, url, **kw):
            self.requests.append((method, url))
            if "evil" in url:
                raise AssertionError("followed a link off the API host")
            return self._reply(200, [{"number": 1, "body": "x"}], {"Link": '<https://evil.example.com/steal?page=2>; rel="next"'})

    assert len(gh(Evil())._paged(f"/repos/{REPO}/issues")) == 1


# -- the Status issue -------------------------------------------------------------------
def three_pages_with_the_status_issue_on_page_two(http):
    """Newest first, 100 per page: 250 issues/PRs, the Status issue is the 150th from the top."""
    status = http.add_issue(statusissue.MARKER + "\n# status", title="sealedstack status")
    for i in range(149):
        http.add_issue("noise", title=f"n{i}", pr=(i % 2 == 0))
    # make it the oldest-but-one, so it sits on page 2 of the newest-first list
    for i in range(100):
        http.issues.insert(0, {"number": http._id(), "title": f"old{i}", "body": ""})
    http.issues.append(http.issues.pop(0))  # shuffle a little; order is irrelevant to the lookup
    return status


def test_the_status_issue_is_found_on_page_two_without_creating_a_duplicate():
    http = PagedGitHub(page_size=100)
    status = three_pages_with_the_status_issue_on_page_two(http)
    http.search_status = 503  # the search API is down: only pagination can find it
    f = gh(http)
    assert f.get_issue(REPO, statusissue.MARKER) == status
    pages = [r for r in http.listed(f"/repos/{REPO}/issues?") if "state=all" in r[1]]
    assert len(pages) >= 2 and any("page=2" in r[1] for r in pages)
    # publishing (what the hourly run does) edits it instead of creating another
    before = len(http.issues)
    statusissue.publish(f, REPO, statusissue.MARKER + "\nupdated")
    assert len(http.issues) == before and not [r for r in http.requests if r[0] == "POST" and r[1].endswith("/issues")]


def test_the_old_first_page_only_read_would_have_missed_it():
    http = PagedGitHub(page_size=100)
    status = three_pages_with_the_status_issue_on_page_two(http)
    first_page = [i for i in list(reversed(http.issues))[:100]]
    assert status not in [i["number"] for i in first_page]  # proves the fixture needs page 2


def test_search_finds_it_with_one_request_when_no_number_is_stored():
    http = PagedGitHub()
    status = three_pages_with_the_status_issue_on_page_two(http)
    assert gh(http).get_issue(REPO, statusissue.MARKER) == status
    assert http.listed("/search/issues") and not http.listed("state=all")  # no scan of every issue


def test_a_stored_number_is_one_request_and_is_checked():
    http = PagedGitHub()
    status = three_pages_with_the_status_issue_on_page_two(http)
    http.variables[statusissue.NUMBER_VARIABLE] = str(status)
    assert gh(http).get_issue(REPO, statusissue.MARKER, variable=statusissue.NUMBER_VARIABLE) == status
    assert not http.listed("/search/issues") and not http.listed("state=all")
    # a stale number (the issue was deleted or is something else) falls back to search
    http.variables[statusissue.NUMBER_VARIABLE] = str(http.add_issue("an unrelated issue"))
    assert gh(http).get_issue(REPO, statusissue.MARKER, variable=statusissue.NUMBER_VARIABLE) == status
    assert http.listed("/search/issues")


def test_pull_requests_are_never_taken_for_the_status_issue():
    http = PagedGitHub()
    http.add_issue(statusissue.MARKER, pr=True)
    http.search_results = [http.issues[0]]
    assert gh(http).get_issue(REPO, statusissue.MARKER) is None


def test_creating_the_status_issue_remembers_its_number():
    http = PagedGitHub()
    f = gh(http)
    number = statusissue.publish(f, REPO, statusissue.MARKER + "\nfirst")
    assert http.variables[statusissue.NUMBER_VARIABLE] == str(number)
    http.search_status = 503
    assert statusissue.publish(f, REPO, statusissue.MARKER + "\nsecond") == number  # found again, not duplicated
    assert len([i for i in http.issues if statusissue.MARKER in i["body"]]) == 1


# -- comments ---------------------------------------------------------------------------------
def test_issue_comments_reads_every_page_oldest_first():
    http = PagedGitHub(page_size=100)
    number = http.add_issue("a busy PR", pr=True)
    http.add_comments(number, 250)
    rows = gh(http).issue_comments(REPO, number)
    assert len(rows) == 250 and rows[0]["body"] == "chatter 0" and rows[-1]["body"] == "chatter 249"
    assert len(http.listed(f"/issues/{number}/comments")) == 3


def test_an_approval_on_a_very_busy_pr_is_seen():
    """The old read took the first 100 comments: an approval at position 180 was missed."""
    http = PagedGitHub(page_size=100)
    number = http.add_issue("PR", pr=True)
    http.add_comments(number, 150)
    http.add_comments(number, 1, body=f"{approval.PLAN_MARKER}\nplan", author="github-actions")
    http.add_comments(number, 30)
    http.add_comments(number, 1, body="/sealedstack approve", author="you")
    assert approval.approved_by_comment(gh(http), REPO, number, ["you"])


def test_upsert_with_a_marker_finds_the_comment_on_a_later_page_and_edits_it():
    http = PagedGitHub(page_size=100)
    number = http.add_issue("PR", pr=True)
    http.add_comments(number, 120)
    http.add_comments(number, 1, body="<!-- m -->\nold")
    f = gh(http)
    f.upsert_comment(REPO, number, "<!-- m -->\nnew", marker="<!-- m -->")
    assert len(http.comments[number]) == 121  # edited in place, not appended
    assert http.comments[number][-1]["body"] == "<!-- m -->\nnew"
    assert [r for r in http.requests if r[0] == "PATCH"]


def test_a_repo_with_few_items_still_needs_one_request():
    http = PagedGitHub()
    number = http.add_issue("PR", pr=True)
    http.add_comments(number, 3)
    gh(http).issue_comments(REPO, number)
    assert len(http.listed(f"/issues/{number}/comments")) == 1
