#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
STATE_DIR="$REPO_ROOT/_projects/_state"
PROJ="rulestest$$"
PROJ2="rulestestnone$$"
PROJ_DIR="$REPO_ROOT/_projects/$PROJ"
SID="rulesinj$$-0000-0000-0000-000000000000"
STATE_FILE="$STATE_DIR/$SID.json"

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  rm -f "$STATE_FILE"
  rm -rf "$PROJ_DIR"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$STATE_DIR" "$PROJ_DIR"

write_plain_rules() {
  cat > "$PROJ_DIR/rules.md" <<'EOF'
# Project rules

## Never edit dist directly
Edit src/ and rebuild.

## Commit messages carry no trailers

```
## Example heading inside a fence — not a rule
```
EOF
}

write_everyturn_rules() {
  cat > "$PROJ_DIR/rules.md" <<'EOF'
---
inject_every_turn: true
max_lines: 100
---
# Project rules

## Always warm rule
EOF
}

run_hook() {
  local prompt="$1" state="$2" payload
  payload=$(uv run --no-project python -c "import json,sys;print(json.dumps({'session_id':'$SID','prompt':sys.argv[1],'transcript_path':''}))" "$prompt")
  printf '%s' "$state" > "$STATE_FILE"
  echo "$payload" | uv run --no-project python "$HOOK" 2>/dev/null || true
}

ctx() {
  uv run --no-project python -c "
import sys, json
d = sys.stdin.read().strip()
if not d:
    print(''); sys.exit(0)
try:
    print(json.loads(d).get('hookSpecificOutput', {}).get('additionalContext', ''))
except Exception:
    print('')
"
}

echo "=== Test: per-project rules.md injection ==="
  echo ""

echo "[Case 1] switch → full primer"
write_plain_rules
STATE1="{\"project\":\"$PROJ\",\"rules_loaded\":true,\"indexed_project\":\"$PROJ\",\"guidelines_loaded\":true,\"project_rules_indexed\":\"\",\"origin\":\"cc\"}"
OUT=$(run_hook "do something" "$STATE1" | ctx)

if echo "$OUT" | grep -q "\[Project Rules: $PROJ\] — full text (read now)"; then
  pass "Case 1: primer header present"
else
  fail "Case 1: primer header missing"
fi
if echo "$OUT" | grep -q "Edit src/ and rebuild."; then
  pass "Case 1: full body injected"
else
  fail "Case 1: full body not injected"
fi
if grep -q "\"project_rules_indexed\": *\"$PROJ\"" "$STATE_FILE"; then
  pass "Case 1: state primed (project_rules_indexed set)"
else
  fail "Case 1: state not primed"
fi

  echo ""
echo "[Case 2] already primed -> ## manifest, fenced heading excluded"
STATE2="{\"project\":\"$PROJ\",\"rules_loaded\":true,\"indexed_project\":\"$PROJ\",\"guidelines_loaded\":true,\"project_rules_indexed\":\"$PROJ\",\"origin\":\"cc\"}"
OUT=$(run_hook "next turn" "$STATE2" | ctx)

if echo "$OUT" | grep -q "\[Project Rules reminder: $PROJ\]"; then
  pass "Case 2: manifest header present"
else
  fail "Case 2: manifest header missing"
fi
if echo "$OUT" | grep -q "^- Never edit dist directly$" && echo "$OUT" | grep -q "^- Commit messages carry no trailers$"; then
  pass "Case 2: real headings listed as bullets"
else
  fail "Case 2: real headings not listed"
fi
if echo "$OUT" | grep -q "Example heading inside a fence"; then
  fail "Case 2: fenced heading leaked into manifest"
else
  pass "Case 2: fenced heading excluded"
fi
if echo "$OUT" | grep -q "full text (read now)"; then
  fail "Case 2: primer text present on subsequent turn"
else
  pass "Case 2: no primer on subsequent turn"
fi

  echo ""
echo "[Case 3] inject_every_turn: true → full body per-turn"
write_everyturn_rules
STATE3="{\"project\":\"$PROJ\",\"rules_loaded\":true,\"indexed_project\":\"$PROJ\",\"guidelines_loaded\":true,\"project_rules_indexed\":\"$PROJ\",\"origin\":\"cc\"}"
OUT=$(run_hook "another turn" "$STATE3" | ctx)

if echo "$OUT" | grep -q "\[Project Rules: $PROJ\] — full text (per-turn)"; then
  pass "Case 3: per-turn full header present"
else
  fail "Case 3: per-turn full header missing"
fi
if echo "$OUT" | grep -q "## Always warm rule"; then
  pass "Case 3: full body injected every turn"
else
  fail "Case 3: full body not injected"
fi
if echo "$OUT" | grep -q "inject_every_turn: true"; then
  fail "Case 3: frontmatter leaked into injected body"
else
  pass "Case 3: frontmatter stripped"
fi

  echo ""
echo "[Case 4] no rules.md → no rules block"
STATE4="{\"project\":\"$PROJ2\",\"rules_loaded\":true,\"indexed_project\":\"$PROJ2\",\"guidelines_loaded\":true,\"project_rules_indexed\":\"\",\"origin\":\"cc\"}"
OUT=$(run_hook "no rules here" "$STATE4" | ctx)

if echo "$OUT" | grep -q "Project Rules"; then
  fail "Case 4: rules block present when no rules.md"
else
  pass "Case 4: no rules block"
fi

  echo ""
echo "=== Done ==="
