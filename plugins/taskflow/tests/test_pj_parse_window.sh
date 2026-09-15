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
mkdir -p "$STATE_DIR"
SID="pjwindow0-0000-4000-8000-000000000000"
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

echo "=== Test: pj:/norouter parse window and charset ==="
  echo ""

run_hook() {
  local prompt="$1"
  local payload
  payload=$(uv run --no-project python -c "
import json, sys
print(json.dumps({'session_id': '$SID', 'prompt': sys.argv[1], 'transcript_path': ''}))
" "$prompt")
mkdir -p "$STATE_DIR"
  echo '{"project":"","rules_loaded":false,"indexed_project":"","guidelines_loaded":false,"origin":"cc"}' > "$STATE_FILE"
  echo "$payload" | uv run --no-project python "$HOOK" 2>/dev/null || true
}

extract_project() {
  local output="$1"
  echo "$output" | uv run --no-project python -c "
import sys, json, re
data = sys.stdin.read().strip()
if not data:
    print('')
    sys.exit(0)
try:
    obj = json.loads(data)
    ctx = obj.get('hookSpecificOutput', {}).get('additionalContext', '')
    m = re.search(r'current_project=(\S*)', ctx)
    print(m.group(1) if m else '')
except Exception:
    print('')
"
}

is_empty_output() {
  local output="$1"
  [ -z "$(echo "$output" | tr -d '[:space:]')" ] && return 0 || return 1
}

echo "[Case 1] pj: beyond 500-char window — should NOT be recognized"

FILLER=$(uv run --no-project python -c "print('x' * 500)")
PROMPT_WINDOW_OUT="${FILLER} pj:window-out-project do something"

OUT=$(run_hook "$PROMPT_WINDOW_OUT")
PROJECT=$(extract_project "$OUT")

if [ -n "$(echo "$OUT" | tr -d '[:space:]')" ]; then
  pass "Case 1: positive marker — the hook produced output, so the empty project is a parse verdict, not a skipped hook"
else
  fail "Case 1: hook produced NO output; the empty-project assertion above is vacuous"
fi
if grep -q '"indexed_project"' "$STATE_FILE"; then
  pass "Case 1: positive marker — the hook rewrote the sandbox state json"
else
  fail "Case 1: sandbox state json not rewritten by the hook: $(cat "$STATE_FILE")"
fi

if [ -z "$PROJECT" ] || [ "$PROJECT" = '""' ] || [ "$PROJECT" = "" ]; then
  pass "Case 1: pj: beyond window not recognized (project='$PROJECT')"
else
  fail "Case 1: pj: beyond window was recognized as project='$PROJECT'"
fi

  echo ""
echo "[Case 2] pj:foo. — trailing dot should be excluded from project name"

PROMPT_TRAILING_DOT="pj:foo. please do something"
OUT=$(run_hook "$PROMPT_TRAILING_DOT")
PROJECT=$(extract_project "$OUT")

if [ "$PROJECT" = "foo" ]; then
  pass "Case 2: trailing dot excluded, project='foo'"
elif [ "$PROJECT" = "foo." ]; then
  fail "Case 2: trailing dot was included in project name (got 'foo.')"
else
  if echo "$PROJECT" | grep -q "^foo$"; then
  pass "Case 2: trailing dot excluded, project='foo'"
else
    fail "Case 2: unexpected project value='$PROJECT'"
fi
fi

  echo ""
echo "[Case 3] norouter beyond 500-char window — should NOT trigger bypass"

FILLER=$(uv run --no-project python -c "print('x' * 500)")
PROMPT_NOROUTER_OUT="pj:testproject ${FILLER} norouter"

OUT=$(run_hook "$PROMPT_NOROUTER_OUT")

if is_empty_output "$OUT"; then
  fail "Case 3: norouter beyond window triggered bypass (empty output)"
else
  pass "Case 3: norouter beyond window ignored, injection occurred"
fi

  echo ""
echo "=== Done ==="
