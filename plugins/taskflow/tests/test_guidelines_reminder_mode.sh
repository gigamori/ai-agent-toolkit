#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
FULL_MD="$REPO_ROOT/plugins/taskflow/prompts/guidelines_reminder.md"
MANIFEST_MD="$REPO_ROOT/plugins/taskflow/prompts/guidelines_reminder_manifest.md"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

echo "=== Test: TASKFLOW_GUIDELINES_REMINDER full/manifest toggle ==="
echo ""

echo "[Case 5] (b) block byte-identical between full and manifest prompt files"
FULL_B=$(grep -E '^(ROUTER:|RESPONSE LEADING LINES:)' "$FULL_MD")
MANIFEST_B=$(grep -E '^(ROUTER:|RESPONSE LEADING LINES:)' "$MANIFEST_MD")
if [ "$FULL_B" = "$MANIFEST_B" ]; then
  pass "Case 5: (b) block matches byte-for-byte"
else
  fail "Case 5: (b) block MISMATCH — manifest drifted from full"
fi

TMP="$(mktemp -d)"
echo ""
echo "=== isolated tmpdir: $TMP ==="
STATE_BEFORE_COUNT=$(find "$REAL_STATE_DIR" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)

cleanup() {
  rm -rf "$TMP"
echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT

cd "$TMP"
PROJ="reminderModeTest"
mkdir -p "_projects/$PROJ" "_projects/_state"
printf '# %s\n\nテスト用プロジェクト\n' "$PROJ" > "_projects/$PROJ/index.md"
printf '# Progress: %s\n\n<!-- @table:begin -->\n<!-- @table:end -->\n' "$PROJ" > "_projects/$PROJ/progress.md"

SID="deadbeef-cafe-4caf-8caf-cafecafecafe"
STATE_FILE="_projects/_state/$SID.json"
STATE_JSON="{\"project\":\"$PROJ\",\"rules_loaded\":true,\"indexed_project\":\"$PROJ\",\"guidelines_loaded\":true,\"project_rules_indexed\":\"\",\"origin\":\"cc\"}"

run_hook() {
  printf '%s' "$STATE_JSON" > "$STATE_FILE"
  local payload
  payload=$(uv run --no-project python -c "import json;print(json.dumps({'session_id':'$SID','prompt':'次のターン','transcript_path':''}))")
  if [ -n "$1" ]; then
    TASKFLOW_GUIDELINES_REMINDER="$1" bash -c "echo '$payload' | uv run --no-project python '$HOOK' 2>/dev/null" || true
else
    echo "$payload" | uv run --no-project python "$HOOK" 2>/dev/null || true
fi
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

FULL_MARKER="no status: or summary: in task frontmatter"
MANIFEST_MARKER="GUIDELINES manifest"

echo ""
echo "[Case 1] env unset -> full reminder"
OUT=$(run_hook "" | ctx)
if echo "$OUT" | grep -qF "$FULL_MARKER" && ! echo "$OUT" | grep -qF "$MANIFEST_MARKER"; then
  pass "Case 1: full reminder injected, manifest absent"
else
  fail "Case 1: unexpected content. OUT=$OUT"
fi

echo ""
echo "[Case 2] env=full -> full reminder (explicit)"
OUT=$(run_hook "full" | ctx)
if echo "$OUT" | grep -qF "$FULL_MARKER" && ! echo "$OUT" | grep -qF "$MANIFEST_MARKER"; then
  pass "Case 2: full reminder injected"
else
  fail "Case 2: unexpected content. OUT=$OUT"
fi

echo ""
echo "[Case 3] env=manifest -> manifest reminder"
OUT=$(run_hook "manifest" | ctx)
if echo "$OUT" | grep -qF "$MANIFEST_MARKER" && ! echo "$OUT" | grep -qF "$FULL_MARKER"; then
  pass "Case 3: manifest reminder injected, full-only prose absent"
else
  fail "Case 3: unexpected content. OUT=$OUT"
fi
if echo "$OUT" | grep -q 'ROUTER: \[Progress Session\]' && echo "$OUT" | grep -q 'RESPONSE LEADING LINES:'; then
  pass "Case 3: (b) ROUTER + RESPONSE LEADING LINES present in manifest mode"
else
  fail "Case 3: (b) block missing in manifest mode. OUT=$OUT"
fi

echo ""
echo "[Case 4] env=bogus -> falls back to full"
OUT=$(run_hook "not-a-real-mode" | ctx)
if echo "$OUT" | grep -qF "$FULL_MARKER" && ! echo "$OUT" | grep -qF "$MANIFEST_MARKER"; then
  pass "Case 4: unknown value falls back to full"
else
  fail "Case 4: unexpected content. OUT=$OUT"
fi

echo ""
echo "[Case 6] real _projects/_state/ untouched by this run"
if [ -e "$REAL_STATE_DIR/$SID.json" ]; then
  fail "Case 6: synthetic session_id leaked into REAL state dir: $REAL_STATE_DIR/$SID.json"
else
  pass "Case 6: synthetic session_id absent from real state dir"
fi
STATE_AFTER_COUNT=$(find "$REAL_STATE_DIR" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)
if [ "$STATE_BEFORE_COUNT" = "$STATE_AFTER_COUNT" ]; then
  pass "Case 6: real state dir .json count unchanged ($STATE_BEFORE_COUNT)"
else
  fail "Case 6: real state dir .json count changed ($STATE_BEFORE_COUNT -> $STATE_AFTER_COUNT)"
fi

echo ""
echo "=== Done ==="
