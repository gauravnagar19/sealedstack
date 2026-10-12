#!/usr/bin/env bash
# scripts/publish-check.sh -- pre-publication privacy guard.
#
# Guarantees that nothing personal is about to be published. "Personal" means
# either a term from the authoritative (gitignored) deny-list
# internal/publish-denylist.txt, or a pattern that looks like real infra:
# IP addresses, MAC addresses, e-mail addresses and long hex IDs.
#
# Usage:
#   scripts/publish-check.sh              content + patterns over tracked files
#   scripts/publish-check.sh --strict     the above, plus gitleaks over `git archive HEAD`
#   scripts/publish-check.sh --metadata   git history/tags: identities, messages, tags
#   scripts/publish-check.sh --colors     force colour
#   scripts/publish-check.sh --no-colors  disable colour (alias: --fixed)
#
# Exit status is non-zero if anything is found. The deny-list terms live only in
# the gitignored file, never inline here.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DENYLIST="${ROOT}/internal/publish-denylist.txt"

usage() {
  cat <<'EOF'
scripts/publish-check.sh -- pre-publication privacy guard.

Scans tracked files (excluding internal/) for terms from the gitignored
deny-list internal/publish-denylist.txt and for personal patterns
(IPs, MACs, e-mail addresses, long hex IDs).

Usage:
  scripts/publish-check.sh              content + patterns over tracked files
  scripts/publish-check.sh --strict     the above, plus gitleaks over `git archive HEAD`
  scripts/publish-check.sh --metadata   git history/tags: identities, messages, tags
  scripts/publish-check.sh --colors     force colour
  scripts/publish-check.sh --no-colors  disable colour (alias: --fixed)

Exit status is non-zero if anything is found. The deny-list terms live only in
the gitignored file, never inline here.
EOF
}

MODE="default"
COLOR="auto"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --strict) MODE="strict" ;;
    --metadata) MODE="metadata" ;;
    --colors) COLOR="always" ;;
    --no-colors) COLOR="never" ;;
    --fixed) COLOR="never" ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'publish-check: unknown option: %s\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "${COLOR}" == "auto" ]]; then
  if [[ -t 1 ]]; then COLOR="always"; else COLOR="never"; fi
fi

paint() {
  # paint <colour-code> <text>
  if [[ "${COLOR}" == "always" ]]; then
    printf '\033[%sm%s\033[0m\n' "$1" "$2"
  else
    printf '%s\n' "$2"
  fi
}
say() { paint "1" "$1"; }
ok() { paint "32" "$1"; }
fail() { paint "31" "$1" >&2; }

status=0

# --- content scan: deny-list terms + personal patterns --------------------
run_content_scan() {
  if ! python3 - "${ROOT}" "${DENYLIST}" <<'PY'
import ipaddress
import os
import pathlib
import re
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
denylist = pathlib.Path(sys.argv[2])

# --- the authoritative deny-list (gitignored, never inline) ---------------
terms = []
if denylist.is_file():
    for raw in denylist.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        terms.append(line)
else:
    print(f"publish-check: no {denylist}; skipping deny-list scan")

# --- tracked files, excluding the private internal/ tree -------------------
listed = subprocess.run(
    ["git", "-C", str(root), "ls-files", "-z"],
    capture_output=True, text=True, check=True,
).stdout
files = [f for f in listed.split("\0") if f and not f.startswith("internal/")]

# --- allowed patterns ------------------------------------------------------
ALLOWED_V4 = [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",      # RFC1918
    "127.0.0.0/8", "169.254.0.0/16",                       # loopback, link-local
    "100.64.0.0/10",                                       # CGNAT (Tailscale)
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",   # RFC5737 documentation
    "0.0.0.0/32", "255.255.255.255/32",
    # Well-known public DNS resolvers used as examples.
    "1.1.1.1/32", "1.0.0.1/32", "8.8.8.8/32", "8.8.4.4/32", "9.9.9.9/32",
)]
ALLOWED_V6 = [ipaddress.ip_network(n) for n in (
    "2001:db8::/32",                                       # documentation
    "::1/128", "::/128",                                   # loopback, unspecified
    "fd00::/8",                                            # unique local
    "fe80::/10",                                           # link-local
)]
ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "users.noreply.github.com"}
ALLOWED_EMAILS = {"noreply@anthropic.com"}
ALLOWED_MACS = {"00:00:00:00:00:00"}

RE_V4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
RE_V6 = re.compile(
    r"(?<![0-9A-Za-z_.:-])((?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4})(?![0-9A-Za-z_.:-])"
)
RE_MAC = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})(?![0-9A-Fa-f])")
RE_EMAIL = re.compile(r"(?<![A-Za-z0-9._%+-])([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
RE_HEX = re.compile(r"(?<![0-9A-Fa-f])([0-9A-Fa-f]{32,})(?![0-9A-Fa-f])")

# Lines that are lock/checksum/known-hash noise, not personal identifiers.
RE_CHECKSUM_LINE = re.compile(
    r"(?:sha(?:1|224|256|384|512|3)|shasum|checksum|integrity|digest|zh:|h[1-3]:)",
    re.IGNORECASE,
)


def is_lock(name):
    base = os.path.basename(name)
    return (
        name.endswith(".lock")
        or name.endswith(".lock.hcl")
        or name.endswith(".terraform.lock.hcl")
        or base in {"package-lock.json", "npm-shrinkwrap.json", "go.sum", "Cargo.lock", "yarn.lock"}
    )


problems = []


def report(msg):
    problems.append(msg)


deny_hits = 0
pattern_hits = 0
for rel in files:
    try:
        data = (root / rel).read_bytes()
    except OSError:
        continue
    if b"\x00" in data:
        continue
    text = data.decode("utf-8", errors="ignore")
    low = text.lower()

    # deny-list terms (case-insensitive), first hit per term per file
    for term in terms:
        needle = term.lower()
        idx = low.find(needle)
        if idx != -1:
            lineno = low.count("\n", 0, idx) + 1
            report(f"FAIL: deny-list term '{term}' at {rel}:{lineno}")
            deny_hits += 1

    lock = is_lock(rel)
    for lineno, line in enumerate(text.splitlines(), 1):
        for match in RE_V4.finditer(line):
            try:
                ip = ipaddress.ip_address(match.group(1))
            except ValueError:
                continue
            if not any(ip in net for net in ALLOWED_V4):
                report(f"FAIL: public/other IPv4 {match.group(1)} at {rel}:{lineno}")
                pattern_hits += 1
        for match in RE_V6.finditer(line):
            candidate = match.group(1)
            if sum(c in "0123456789abcdefABCDEF" for c in candidate) < 3:
                continue  # too few hex digits: a slice like raw[0::2], not an address
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if not any(ip in net for net in ALLOWED_V6):
                report(f"FAIL: public/other IPv6 {candidate} at {rel}:{lineno}")
                pattern_hits += 1
        for match in RE_MAC.finditer(line):
            mac = match.group(1).lower()
            if mac not in ALLOWED_MACS:
                report(f"FAIL: MAC address {match.group(1)} at {rel}:{lineno}")
                pattern_hits += 1
        for match in RE_EMAIL.finditer(line):
            email = match.group(1)
            if line[match.end():match.end() + 1] == ":":
                continue  # scp-style SSH remote (user@host:path), not an e-mail
            _, _, domain = email.rpartition("@")
            if domain.lower() in ALLOWED_EMAIL_DOMAINS or email.lower() in ALLOWED_EMAILS:
                continue
            report(f"FAIL: e-mail address {email} at {rel}:{lineno}")
            pattern_hits += 1
        if lock or RE_CHECKSUM_LINE.search(line):
            continue
        for match in RE_HEX.finditer(line):
            value = match.group(1)
            if rel.endswith((".md", ".mdx", ".rst")) and len(value) == 40:
                continue  # a git object id quoted in docs
            report(f"FAIL: long hex id {value} at {rel}:{lineno}")
            pattern_hits += 1

print("== deny-list terms ==")
print(f"ok: no deny-list terms in tracked files" if deny_hits == 0 else f"FAIL: {deny_hits} deny-list hit(s)")
print("== personal patterns ==")
print(f"ok: no unwanted IPv4/IPv6/MAC/e-mail/hex patterns" if pattern_hits == 0 else f"FAIL: {pattern_hits} pattern hit(s)")

if problems:
    print()
    for p in problems:
        print(p)
    sys.exit(1)
sys.exit(0)
PY
  then
    status=1
  fi
}

# --- metadata scan: identities, commit messages, tags ---------------------
run_metadata_scan() {
  if ! python3 - "${ROOT}" "${DENYLIST}" <<'PY'
import pathlib
import subprocess
import sys

root = sys.argv[1]
denylist = pathlib.Path(sys.argv[2])

terms = []
if denylist.is_file():
    for raw in denylist.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            terms.append(line.lower())
else:
    print(f"publish-check: no {denylist}; skipping deny-list scan")


def git(*args):
    return subprocess.run(
        ["git", "-C", root, *args], capture_output=True, text=True, check=True,
    ).stdout


def has_term(value):
    low = value.lower()
    return sorted({t for t in terms if t in low})


def neutral(name, email):
    name_l, email_l = name.lower(), email.lower()
    if email_l.endswith("@users.noreply.github.com"):
        return True
    if "dependabot" in name_l or "dependabot" in email_l:
        return True
    if "github-actions" in name_l or "github-actions" in email_l:
        return True
    return name_l == "sealedstack maintainers"


problems = []
bad_identity = {}
identity_terms = {}
message_terms = {}
tag_terms = {}


def sample(shas):
    shas = list(shas)
    shown = ", ".join(s[:12] for s in shas[:3])
    return shown if len(shas) <= 3 else f"{shown} (+{len(shas) - 3} more)"


# Commits: author and committer identity + the full message.
sep = "\x1f"
end = "\x1e"
fmt = sep.join(["%H", "%an", "%ae", "%cn", "%ce", "%B"]) + end
log = git("log", f"--format={fmt}")
commits = 0
for record in log.split(end):
    record = record.strip("\n")
    if not record:
        continue
    commits += 1
    parts = record.split(sep)
    sha = parts[0]
    an, ae, cn, ce = parts[1], parts[2], parts[3], parts[4]
    message = sep.join(parts[5:]) if len(parts) > 5 else ""
    for who, name, email in (("author", an, ae), ("committer", cn, ce)):
        if not neutral(name, email):
            bad_identity.setdefault((who, name, email), set()).add(sha)
        for term in has_term(f"{name} <{email}>"):
            identity_terms.setdefault((term, who), set()).add(sha)
    for term in has_term(message):
        message_terms.setdefault(term, set()).add(sha)

# Tags: names, tagger identity and annotation body.
tag_fmt = sep.join(["%(refname:short)", "%(taggername)", "%(taggeremail)", "%(contents)"])
tags = 0
raw_tags = git("for-each-ref", f"--format={tag_fmt}", "refs/tags")
for line in raw_tags.splitlines():
    if not line:
        continue
    tags += 1
    fields = line.split(sep)
    name = fields[0]
    tagger = fields[1] if len(fields) > 1 else ""
    tagger_email = fields[2] if len(fields) > 2 else ""
    contents = sep.join(fields[3:]) if len(fields) > 3 else ""
    if tagger and not neutral(tagger, tagger_email):
        bad_identity.setdefault(("tagger", tagger, tagger_email), set()).add(name)
    for term in has_term(f"{name} {tagger} {tagger_email} {contents}"):
        tag_terms.setdefault(term, set()).add(name)

print("== git metadata ==")
print(f"scanned {commits} commit(s) and {tags} tag(s)")
for (term, who), shas in sorted(identity_terms.items()):
    problems.append(f"FAIL: deny-list term '{term}' in {who} identity ({len(shas)} commit(s)) [{sample(shas)}]")
for term, shas in sorted(message_terms.items()):
    problems.append(f"FAIL: deny-list term '{term}' in commit message(s) ({len(shas)}) [{sample(shas)}]")
for term, names in sorted(tag_terms.items()):
    problems.append(f"FAIL: deny-list term '{term}' in tag(s) [{sample(names)}]")
for (who, name, email), shas in sorted(bad_identity.items()):
    problems.append(
        f"FAIL: non-neutral {who} identity: {name} <{email}> in {len(shas)} place(s) [{sample(shas)}]"
    )

if problems:
    print()
    for p in problems:
        print(p)
    sys.exit(1)
print("ok: all commit/tag identities are neutral and no deny-list terms in metadata")
sys.exit(0)
PY
  then
    status=1
  fi
}

# --- gitleaks over the exported tree (strict only) ------------------------
TMPDIR_PC=""
run_gitleaks() {
  if ! command -v gitleaks >/dev/null 2>&1; then
    fail "FAIL: gitleaks is not installed (required by --strict)"
    status=1
    return
  fi
  TMPDIR_PC="$(mktemp -d)"
  trap 'rm -rf "${TMPDIR_PC}"' EXIT
  git -C "${ROOT}" archive HEAD | tar -x -C "${TMPDIR_PC}"
  say "== gitleaks (git archive HEAD) =="
  if gitleaks detect --no-git --source "${TMPDIR_PC}" --redact --verbose; then
    ok "ok: gitleaks found no leaks"
  else
    fail "FAIL: gitleaks reported leaks"
    status=1
  fi
}

# --- dispatch -------------------------------------------------------------
case "${MODE}" in
  metadata)
    run_metadata_scan
    ;;
  strict)
    run_content_scan
    run_gitleaks
    ;;
  *)
    run_content_scan
    ;;
esac

if [[ "${status}" -eq 0 ]]; then
  ok "publish-check: ok (${MODE})"
else
  fail "publish-check: FAILED (${MODE})"
fi
exit "${status}"
