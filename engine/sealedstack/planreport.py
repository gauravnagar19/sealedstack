"""Turn raw OpenTofu plan output into the PR plan comment (UX §8).

The comment carries ``approval.PLAN_MARKER`` so it is updated in place, and its
text is deterministic (no timestamps), so an unchanged plan leaves the comment --
and therefore the approvals given after it (D-052) -- untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .approval import PLAN_MARKER

MAX_DETAIL = 45_000  # a GitHub comment is limited to 65,536 characters

_ADDR = re.compile(
    r"^\s*# (?P<addr>\S+?)(?: \(.*?\))? (?P<what>will be created|will be updated in-place|"
    r"must be replaced|will be destroyed|will be read during apply)",
    re.MULTILINE,
)
_TOTALS = re.compile(r"Plan: (\d+) to add, (\d+) to change, (\d+) to destroy")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Resources whose destruction loses data.
STATEFUL = re.compile(r"postgres|mysql|mariadb|mongo|redis|database|bucket|backup|volume", re.IGNORECASE)


@dataclass
class Summary:
    create: int = 0
    change: int = 0
    destroy: int = 0
    resources: list = field(default_factory=list)  # [(symbol, address)]
    stateful_destroys: list = field(default_factory=list)

    @property
    def no_changes(self) -> bool:
        return not (self.create or self.change or self.destroy)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse(output: str) -> Summary:
    text = strip_ansi(output)
    summary = Summary()
    for match in _TOTALS.finditer(text):
        summary.create += int(match.group(1))
        summary.change += int(match.group(2))
        summary.destroy += int(match.group(3))
    symbols = {
        "will be created": "+",
        "will be updated in-place": "~",
        "must be replaced": "±",
        "will be destroyed": "-",
    }
    seen = set()
    for match in _ADDR.finditer(text):
        what = match.group("what")
        if what not in symbols:
            continue
        key = (match.group("addr"), what)
        if key in seen:
            continue
        seen.add(key)
        symbol = symbols[what]
        summary.resources.append((symbol, match.group("addr")))
        if symbol in ("-", "±") and STATEFUL.search(match.group("addr")):
            summary.stateful_destroys.append(match.group("addr"))
    return summary


def _details(output: str) -> str:
    text = strip_ansi(output).strip()
    if len(text) > MAX_DETAIL:
        text = text[:MAX_DETAIL] + f"\n… (truncated; {len(text) - MAX_DETAIL} more characters in the job log)"
    return text.replace("</details>", "<\\/details>")


def render_plan(output: str, *, pr: int = 0, rc: int = 0) -> str:
    title = f"## sealedstack plan{f' · PR #{pr}' if pr else ''}"
    if rc != 0:
        tail = "\n".join(strip_ansi(output).strip().splitlines()[-40:])
        return "\n".join(
            [PLAN_MARKER, title, "", f"❌ The plan failed (exit {rc}). Nothing was changed.", "",
             "<details><summary>Last lines of the job output</summary>", "", "```", tail.replace("```", "'''"), "```", "", "</details>"]
        )
    summary = parse(output)
    lines = [PLAN_MARKER, title, ""]
    if summary.no_changes:
        lines.append("No changes. The infrastructure matches the configuration.")
    else:
        lines.append(f"{summary.create} to create · {summary.change} to change · {summary.destroy} to destroy")
        for symbol, address in summary.resources:
            lines.append(f"  {symbol} {address}")
    if summary.stateful_destroys:
        lines += ["", "🛑 This plan destroys stateful resources (data would be lost): "
                  + ", ".join(f"`{a}`" for a in summary.stateful_destroys)
                  + ". Add an explicit `allow_destroy:` entry in this PR only if you really mean it."]
    elif summary.destroy:
        lines += ["", f"⚠ This plan destroys {summary.destroy} resource(s). Review the list above before merging."]
    lines += ["", "<details><summary>Full OpenTofu plan</summary>", "", "```", _details(output).replace("```", "'''"), "```", "", "</details>", "",
              "Merge, then an approver comments `/sealedstack approve` (or dispatches `apply`) to apply."]
    return "\n".join(lines)
