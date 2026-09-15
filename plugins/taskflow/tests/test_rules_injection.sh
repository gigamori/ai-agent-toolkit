#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"

TMP="$(mktemp -d)" || { echo "ABORT: mktemp -d failed" >&2; exit 2; }
[ -n "$TMP" ] && [ -d "$TMP" ] || { echo "ABORT: mktemp -d yielded no usable dir ('$TMP')" >&2; exit 2; }
cd "$TMP" || { echo "ABORT: cd '$TMP' failed" >&2; rm -rf "$TMP"; exit 2; }

case "$TMP" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ABORT: temp workspace $TMP is INSIDE the repo tree ($REPO_ROOT);" \
         "the hooks' ancestor walk would resolve into the real _projects/_state." >&2
    cd /; rm -rf "$TMP"; exit 2 ;;
esac
d="$TMP"
while :; do
  if [ -d "$d/_projects/_state" ]; then
    echo "ABORT: ancestor $d of temp workspace holds _projects/_state;" \
         "the hooks' ancestor walk would reach it." >&2
    cd /; rm -rf "$TMP"; exit 2
  fi
  p="$(dirname "$d")"; [ "$p" = "$d" ] && break; d="$p"
done

STATE_DIR="$TMP/_projects/_state"
PROJ="rulestest"
PROJ2="rulestestnone"
PROJ_DIR="$TMP/_projects/$PROJ"
SID="rulesinj0-0000-4000-8000-000000000000"
STATE_FILE="$STATE_DIR/$SID.json"

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  cd "$REPO_ROOT"
  rm -rf "$TMP"
  LEAKED=""
  for f in "$REAL_STATE_DIR/$SID".*; do
    [ -e "$f" ] && LEAKED="$LEAKED $(basename "$f")"
  done
  if [ -z "$LEAKED" ]; then
    pass "real _projects/_state/ carries no $SID artifact (queried at full 36-char width)"
  else
    fail "synthetic session leaked into the real state dir:$LEAKED"
  fi
  for p in "$REPO_ROOT/_projects/$PROJ" "$REPO_ROOT/_projects/$PROJ2"; do
    if [ -d "$p" ]; then
      fail "test project leaked into the real _projects/ tree: $p"
    else
      pass "real _projects/ carries no $(basename "$p") directory"
    fi
  done
  REAL_STATE_AFTER=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
  if [ "$REAL_STATE_BEFORE" = "$REAL_STATE_AFTER" ]; then
    pass "real _projects/_state/ file count unchanged ($REAL_STATE_BEFORE -> $REAL_STATE_AFTER)"
  else
    fail "real _projects/_state/ file count CHANGED ($REAL_STATE_BEFORE -> $REAL_STATE_AFTER)"
  fi
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
  [ "$FAIL" -eq 0 ] || exit 1
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

if [ -n "$(echo "$OUT" | tr -d '[:space:]')" ]; then
  pass "Case 4: positive marker — the hook still injected context for the rules-less project, so the absent rules block is a verdict, not a skipped hook"
else
  fail "Case 4: hook produced NO context; the no-rules-block assertion below is vacuous"
fi
if echo "$OUT" | grep -q "Project Rules"; then
  fail "Case 4: rules block present when no rules.md"
else
  pass "Case 4: no rules block"
fi

  echo ""
echo "=== Done ==="
