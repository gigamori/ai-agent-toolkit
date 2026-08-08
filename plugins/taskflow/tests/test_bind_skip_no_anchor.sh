#!/usr/bin/env bash
# test_bind_skip_no_anchor.sh — T-D3-2: an ambiguously-damaged task md stays
# UNBOUND, and that residue is now VISIBLE
# (project-notes/specs/capture-detection-gaps.md §4.3, D3).
#
# D3 §4.2 makes the "no `@log` markers at all" shape bindable by GENERATING the
# block (covered by tests/test_log_anchor_generation.py). What remains
# unbindable is ambiguous damage — here TWO `<!-- @log:begin -->` and no
# `<!-- @log:end -->`, which `repair_log_markers` refuses (count != 1) and which
# the generation branch must NOT touch (a begin marker IS present). Before §4.3
# that residue was dropped silently on the touched side, unlike its exec-bind
# twin at `auto-skip(ambiguous)`. This test pins the new behaviour:
#
#   1. the file stays byte-for-byte unmodified (no second block generated)
#   2. stderr carries `[progress capture] bind-skip(no-anchor): <rel> [s:<sid8>]`
#   3. the block reason carries the same `bind-skip(no-anchor)` line
#   4. the task lands in `capture.tried_tasks` in the `<sid>.bind` sidecar
#   5. a LATER Stop does NOT re-report it (INV-1 boundedness: reported once)
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted 250 real
# session-state files there).
#
# Usage:  bash plugins/taskflow/tests/test_bind_skip_no_anchor.sh
# Exit:   0 = all pass, 1 = failure
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# Force immediate capture expiry so the request → G-backstop path completes in
# two Stops without a 15 s wall-clock wait (same hook the other suites use).
export TASKFLOW_CAPTURE_EXPIRY_S=0

HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS_DIR="$TMP/_projects"
STATE_DIR="$PROJECTS_DIR/_state"

PROJECT_NAME="_test-bindskip-$$"
PROJECT_DIR="$PROJECTS_DIR/$PROJECT_NAME"
SID="bndskip$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
STATE_FILE="$STATE_DIR/$SID.json"
BIND_FILE="$STATE_DIR/$SID.bind"
TOUCHED_FILE="$STATE_DIR/$SID.touched"

to_win() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi
}

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

cleanup() {
  if [ -e "$REAL_STATE_DIR/$SID.json" ] || [ -e "$REAL_STATE_DIR/$SID.touched" ] \
     || [ -e "$REAL_STATE_DIR/$SID.bind" ] || [ -e "$REAL_STATE_DIR/$SID.capture" ]; then
    fail "real _projects/_state/ was touched by this test run (session $SID leaked there)"
  else
    pass "real _projects/_state/ untouched (session $SID artifacts never created there)"
  fi
  REAL_STATE_AFTER=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
  if [ "$REAL_STATE_BEFORE" = "$REAL_STATE_AFTER" ]; then
    pass "real _projects/_state/ file count unchanged ($REAL_STATE_BEFORE -> $REAL_STATE_AFTER)"
  else
    fail "real _projects/_state/ file count CHANGED ($REAL_STATE_BEFORE -> $REAL_STATE_AFTER)"
  fi
  cd "$REPO_ROOT"
  rm -rf "$TMP"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT

echo "=== T-D3-2: bind-skip(no-anchor) visibility for ambiguous @log damage ==="
echo "  project:  $PROJECT_DIR"
echo "  session:  $SID  (sid8=$SID8)"
echo "  isolated tempdir: $TMP"
echo ""

mkdir -p "$PROJECT_DIR/tasks/1_in_progress" "$STATE_DIR"

cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"$PROJECT_NAME","timestamp":"2026-08-08T00:00:00"}
EOF

# Ambiguous damage: TWO @log:begin, NO @log:end. repair_log_markers requires
# exactly one begin, so it returns None; the D3 generation branch must not fire
# because a begin marker IS present. Net: bind must fail.
TASK="$PROJECT_DIR/tasks/1_in_progress/2026-08-08_ambiguous.md"
cat > "$TASK" << 'TASK_EOF'
---
priority: HIGH
created: 2026-08-08
updated: 2026-08-08
---

# Ambiguously damaged task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-08: created

<!-- @log:begin -->
- 2026-08-08: a hand edit duplicated the begin marker and ate the end marker
TASK_EOF
# The hook reports `_rel(path, cwd)` and cwd is the temp workspace root (the
# `_projects` parent), so the reported form is repo-relative — the same string
# `touched_capture.py` writes into the ledger.
TASK_REL="_projects/$PROJECT_NAME/tasks/1_in_progress/2026-08-08_ambiguous.md"
printf '%s\n' "$TASK_REL" >> "$TOUCHED_FILE"

sha() { uv run --no-project python -c "import hashlib,sys;print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "$1"; }
BEFORE_HASH="$(sha "$TASK")"

OUT_FILE="$TMP/out.txt"
ERR_FILE="$TMP/err.txt"
invoke_hook() {  # stdout -> $OUT_FILE, stderr -> $ERR_FILE; echoes stdout
  TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys;sys.stdout.write(json.dumps({'session_id':os.environ['TASKFLOW_SID']}))" \
    | uv run --no-project python "$(to_win "$HOOK")" > "$OUT_FILE" 2> "$ERR_FILE"
  cat "$OUT_FILE"
}

# ---------------------------------------------------------------
# Stop #1 — capture requested (spawn block); no backstop attempt yet.
# ---------------------------------------------------------------
echo "[Stop #1] capture requested"
OUT1=$(invoke_hook)
echo "$OUT1" | grep -q '"decision": *"block"' \
  && pass "Stop#1 requests capture (decision:block spawn)" \
  || fail "Stop#1 did not request capture: $OUT1"
grep -q "bind-skip(no-anchor)" "$ERR_FILE" \
  && fail "Stop#1 reported bind-skip before the backstop ran: $(cat "$ERR_FILE")" \
  || pass "Stop#1 does not report bind-skip yet (backstop has not run)"

# ---------------------------------------------------------------
# Stop #2 — capture expired -> G backstop attempts the bind and fails.
# ---------------------------------------------------------------
echo ""
echo "[Stop #2] capture expired -> G backstop bind fails, residue surfaced"
OUT2=$(invoke_hook)
ERR2="$(cat "$ERR_FILE")"

[ "$(sha "$TASK")" = "$BEFORE_HASH" ] \
  && pass "ambiguous task md left byte-for-byte unmodified (no block generated)" \
  || fail "ambiguous task md was modified by the hook"
grep -q "\[s:$SID8\]" "$TASK" \
  && fail "an [s:$SID8] line leaked into the ambiguous task" \
  || pass "no [s:$SID8] line written (bind genuinely failed)"

# NOTE: the message's em-dash is not encodable in the win32 console codepage, so
# Python's stderr backslashreplace handler emits it as a literal `—`. Assert
# the two ASCII halves around it rather than the raw character (the same is true
# of the pre-existing sweep-cap warning line).
echo "$ERR2" | grep -qF "[progress capture] bind-skip(no-anchor): $TASK_REL [s:$SID8]" \
  && pass "stderr carries the bind-skip(no-anchor) line with rel path + sid8" \
  || fail "stderr bind-skip line missing/wrong: $ERR2"
echo "$ERR2" | grep -qF "no writable <!-- @log:begin/end --> block; left unbound." \
  && pass "stderr line explains the cause (no writable @log block)" \
  || fail "stderr cause clause missing: $ERR2"

echo "$OUT2" | grep -q '"decision": *"block"' \
  && pass "Stop#2 blocks to report the residue (INV-1 b)" \
  || fail "Stop#2 did not block: $OUT2"
echo "$OUT2" | grep -qF "[progress capture] bind-skip(no-anchor): $TASK_REL" \
  && pass "block reason carries the bind-skip(no-anchor) F5 line" \
  || fail "block reason missing bind-skip line: $OUT2"
COUNT2=$(echo "$OUT2" | grep -oF "bind-skip(no-anchor): $TASK_REL" | wc -l)
[ "$COUNT2" = "1" ] \
  && pass "block reason reports the task exactly once (got $COUNT2)" \
  || fail "block reason reported the task $COUNT2 times (expected 1)"

# tried_tasks 打止め record in the .bind sidecar.
TRIED=$(uv run --no-project python - "$BIND_FILE" << 'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    print("NOBIND"); raise SystemExit
print(",".join((d.get("capture") or {}).get("tried_tasks") or []))
PY
)
[ "$TRIED" = "2026-08-08_ambiguous.md" ] \
  && pass ".bind capture.tried_tasks records the unbindable task: $TRIED" \
  || fail ".bind tried_tasks wrong: '$TRIED'"

# ---------------------------------------------------------------
# Stop #3 — bounded: the same residue is NOT re-reported (INV-1).
# ---------------------------------------------------------------
echo ""
echo "[Stop #3] boundedness: no re-report, no loop"
OUT3=$(invoke_hook)
ERR3="$(cat "$ERR_FILE")"
if [ -z "$OUT3" ] || ! echo "$OUT3" | grep -q '"decision": *"block"'; then
  pass "Stop#3 does NOT block again (bounded by tried_tasks, no loop)"
else
  fail "Stop#3 re-blocked (unbounded re-report): $OUT3"
fi
echo "$ERR3" | grep -q "bind-skip(no-anchor)" \
  && fail "Stop#3 re-emitted the stderr bind-skip line: $ERR3" \
  || pass "Stop#3 does not re-emit the stderr bind-skip line"

OUT4=$(invoke_hook)
if [ -z "$OUT4" ] || ! echo "$OUT4" | grep -q '"decision": *"block"'; then
  pass "Stop#4 still silent (stable)"
else
  fail "Stop#4 blocked: $OUT4"
fi

echo ""
echo "=== Done ==="
