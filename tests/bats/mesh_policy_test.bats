#!/usr/bin/env bats
# N4 / F1: the Tailscale policy guard and passthrough of untouched sections.

setup() {
  REPO="$(cd "$BATS_TEST_DIRNAME/../.." && pwd)"
  CFG="$BATS_TEST_TMPDIR/config"
  OUT="$REPO/.build/tailscale-policy.json"
  mkdir -p "$CFG"
  rm -f "$OUT"
}

teardown() {
  rm -f "$REPO/.build/tailscale-policy.json"
}

@test "N4: managed policy without the file exits 1" {
  cat > "$CFG/stack.yaml" <<'YAML'
slots: { mesh: tailscale }
mesh: { policy: managed }
YAML
  run python3 "$REPO/scripts/mesh-policy.py" --config-dir "$CFG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"is missing"* ]]
}

@test "F1/N4: untouched sections pass through and sealedstack rules are appended" {
  cat > "$CFG/stack.yaml" <<'YAML'
slots: { mesh: tailscale }
mesh: { policy: managed }
YAML
  cat > "$CFG/tailscale-policy.hujson" <<'HUJSON'
{
  // the operator's own policy
  "groups": { "group:admins": ["alice@example.com"] },
  "ssh": [ { "action": "accept", "src": ["group:admins"], "dst": ["tag:server"], "users": ["root"] } ],
  "nodeAttrs": [ { "target": ["tag:server"], "attr": ["funnel"] } ],
  "grants": [ { "src": ["group:admins"], "dst": ["tag:server"], "ip": ["tcp:3000"] } ],
}
HUJSON
  run python3 "$REPO/scripts/mesh-policy.py" --config-dir "$CFG"
  [ "$status" -eq 0 ]
  [ -f "$OUT" ]

  run jq -e '.groups["group:admins"] == ["alice@example.com"]' "$OUT"
  [ "$status" -eq 0 ]
  run jq -e '.ssh[0].users == ["root"]' "$OUT"
  [ "$status" -eq 0 ]
  run jq -e '.nodeAttrs[0].attr == ["funnel"]' "$OUT"
  [ "$status" -eq 0 ]
  run jq -e 'any(.grants[]; .src == ["tag:ci"] and .ip == ["tcp:3000"])' "$OUT"
  [ "$status" -eq 0 ]
  run jq -e '.tagOwners["tag:server"] == ["autogroup:admin"]' "$OUT"
  [ "$status" -eq 0 ]
}

@test "F1: an allow-all policy is refused" {
  cat > "$CFG/stack.yaml" <<'YAML'
slots: { mesh: tailscale }
mesh: { policy: managed }
YAML
  cat > "$CFG/tailscale-policy.hujson" <<'HUJSON'
{ "acls": [ { "action": "accept", "src": ["*"], "dst": ["*"] } ] }
HUJSON
  run python3 "$REPO/scripts/mesh-policy.py" --config-dir "$CFG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"allow"* ]]
}

@test "F1: narrow_allow_all rewrites the allow-all rule" {
  cat > "$CFG/stack.yaml" <<'YAML'
slots: { mesh: tailscale }
mesh: { policy: managed, narrow_allow_all: true }
YAML
  cat > "$CFG/tailscale-policy.hujson" <<'HUJSON'
{ "acls": [ { "action": "accept", "src": ["*"], "dst": ["*"] } ] }
HUJSON
  run python3 "$REPO/scripts/mesh-policy.py" --config-dir "$CFG"
  [ "$status" -eq 0 ]

  run jq -e '.acls[0].src == ["autogroup:member"] and .acls[0].dst == ["autogroup:member:*"]' "$OUT"
  [ "$status" -eq 0 ]
}
