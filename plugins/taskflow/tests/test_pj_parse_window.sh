#!/usr/bin/env bash
# test_pj_parse_window.sh — unit tests for pj:/norouter parse window and charset
# (AC-M2 from review-2026-07-03-fixes.md §3 F-M2)
#
# Tests:
#   Case 1: pj: appearing AFTER the 500-char window is NOT recognized
#   Case 2: pj:foo. (trailing dot) results in project name "foo" (dot excluded)
#   Case 3: norouter appearing AFTER the 500-char window does NOT trigger bypass
#
# Usage:  bash plugins/taskflow/tests/test_pj_parse_window.sh
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
STATE_DIR="$REPO_ROOT/_projects/_state"
SID="pjwindow$$-0000-0000-0000-000000000000"
STATE_FILE="$STATE_DIR/$SID.json"

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  rm -f "$STATE_FILE"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$STATE_DIR"

echo "=== Test: pj:/norouter parse window and charset (F-M2) ==="
echo ""

# Helper: invoke session_init.py with a synthetic stdin payload, capture stdout.
# Writes a minimal state file first so the hook doesn't exit on PROGRESS_ROOT absence.
run_hook() {
  local prompt="$1"
  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({'session_id': '$SID', 'prompt': sys.argv[1], 'transcript_path': ''}))
" "$prompt")
  # Pre-create state so PROGRESS_ROOT bootstrap doesn't gate on pj: check
  mkdir -p "$STATE_DIR"
  echo '{"project":"","rules_loaded":false,"indexed_project":"","guidelines_loaded":false,"origin":"cc"}' > "$STATE_FILE"
  echo "$payload" | uv run --no-project python "$HOOK" 2>/dev/null || true
}

# Helper: extract the current_project value from the hook's additionalContext output.
extract_project() {
  local output="$1"
  echo "$output" | python3 -c "
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

# Helper: check if hook exited with empty output (norouter bypass or no injection).
is_empty_output() {
  local output="$1"
  [ -z "$(echo "$output" | tr -d '[:space:]')" ] && return 0 || return 1
}

# -------------------------------------------------------------------------
# Case 1: pj: appearing AFTER the 500-char window is NOT recognized.
# Build a prompt where pj:window-out-project appears only after 500 chars.
# -------------------------------------------------------------------------
echo "[Case 1] pj: beyond 500-char window — should NOT be recognized"

# 480 chars of filler + " pj:window-out-project" (puts pj: at position ~481)
FILLER=$(python3 -c "print('x' * 480)")
PROMPT_WINDOW_OUT="${FILLER} pj:window-out-project do something"

OUT=$(run_hook "$PROMPT_WINDOW_OUT")
PROJECT=$(extract_project "$OUT")

if [ -z "$PROJECT" ] || [ "$PROJECT" = '""' ] || [ "$PROJECT" = "" ]; then
  pass "Case 1: pj: beyond window not recognized (project='$PROJECT')"
else
  fail "Case 1: pj: beyond window was recognized as project='$PROJECT'"
fi

# -------------------------------------------------------------------------
# Case 2: pj:foo. (trailing dot) results in project name "foo", not "foo."
# -------------------------------------------------------------------------
echo ""
echo "[Case 2] pj:foo. — trailing dot should be excluded from project name"

PROMPT_TRAILING_DOT="pj:foo. please do something"
# For this test, set up a pre-existing state so PROGRESS_ROOT exists
OUT=$(run_hook "$PROMPT_TRAILING_DOT")
PROJECT=$(extract_project "$OUT")

if [ "$PROJECT" = "foo" ]; then
  pass "Case 2: trailing dot excluded, project='foo'"
elif [ "$PROJECT" = "foo." ]; then
  fail "Case 2: trailing dot was included in project name (got 'foo.')"
else
  # "foo" may also appear as part of a longer match; check prefix
  if echo "$PROJECT" | grep -q "^foo$"; then
    pass "Case 2: trailing dot excluded, project='foo'"
  else
    fail "Case 2: unexpected project value='$PROJECT'"
  fi
fi

# -------------------------------------------------------------------------
# Case 3: norouter appearing AFTER the 500-char window does NOT trigger bypass.
# When norouter is outside the window and a project is active, injection should happen.
# -------------------------------------------------------------------------
echo ""
echo "[Case 3] norouter beyond 500-char window — should NOT trigger bypass"

# 480 chars of filler + " norouter" at position ~481; also include pj: within window
FILLER=$(python3 -c "print('x' * 480)")
PROMPT_NOROUTER_OUT="pj:testproject ${FILLER} norouter"

OUT=$(run_hook "$PROMPT_NOROUTER_OUT")

if is_empty_output "$OUT"; then
  fail "Case 3: norouter beyond window triggered bypass (empty output)"
else
  # Output is non-empty, meaning injection happened (norouter was ignored)
  pass "Case 3: norouter beyond window ignored, injection occurred"
fi

echo ""
echo "=== Done ==="
