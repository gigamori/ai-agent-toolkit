#!/usr/bin/env bash
# test_e2e_capture_bind.sh — end-to-end pipeline test.
#
# Chains the REAL PostToolUse capture hook (touched_capture.py) and the Stop hook
# (session_progress_capture.py) through the real `<sid>.touched` artifact, proving
# the capture → bind pipeline. The unit suites write `.touched` directly; this one
# PRODUCES it via touched_capture.py, then consumes it via the Stop hook.
#
#   Stage 1  touched_capture.py records a Write into `<sid>.touched`
#   Stage 2  Stop Round1 → block reminder (not yet bound)
#   Stage 3  Stop Round2 → `[s:<sid8>]` appended end-to-end
#   Stage 4  exec-binding: a task NOT in `.touched` is bound via a `[tasks:]` carry
#   Stage 5  F7a membership containment: an out-of-request sidecar entry is skipped
#
# Usage:  bash plugins/taskflow/tests/test_e2e_capture_bind.sh
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"
# note-task-link.md §10 option-a: Round2 placeholder now backstops on capture
# expiry. Force immediate expiry so the capture-spawn request (Stage 2) is
# followed by the deterministic backstop bind (Stage 3) without a 15s wait.
export TASKFLOW_CAPTURE_EXPIRY_S=0
CAP="$REPO_ROOT/plugins/taskflow/hooks/touched_capture.py"
STOP="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
PROJECTS="$REPO_ROOT/_projects"
STATE="$PROJECTS/_state"
PROJ="_e2e-cap-$$"
PDIR="$PROJECTS/$PROJ"
SID="e2ecap$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"

to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  rm -rf "$PDIR"; rm -f "$SF" "$TF" "$BF" "$STATE/$SID.capture"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$PDIR/tasks/1_in_progress" "$STATE"

cat > "$SF" << EOF
{"session_id":"$SID","project":"$PROJ"}
EOF

mk() {  # $1 = path
  cat > "$1" << 'T'
---
priority: HIGH
created: 2026-06-29
updated: 2026-06-29
---

# E2E task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-06-29: created
<!-- @log:end -->
T
}

cap() {  # $1 = tool_input JSON — invoke the real PostToolUse capture hook
  printf '{"session_id":"%s","tool_input":%s}' "$SID" "$1" | uv run python "$(to_win "$CAP")"
}
stop() {  # $1 = optional last_assistant_message — invoke the real Stop hook
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run python "$(to_win "$STOP")"
}
sidlines() {  # $1 = task md path → count [s:SID8] inside @log block
  uv run python - "$1" "$SID8" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

echo "=== E2E: touched_capture.py → <sid>.touched → session_progress_capture.py ==="
echo "  project=$PROJ  sid8=$SID8"
echo ""

TASK="$PDIR/tasks/1_in_progress/2026-06-29_e2e.md"
mk "$TASK"

# Stage 1 — capture a Write through the real PostToolUse hook.
echo "[Stage 1] touched_capture records the Write"
cap "{\"file_path\":\"$(to_win "$TASK")\"}"
if [ -f "$TF" ] && grep -q "_projects/$PROJ/tasks/1_in_progress/2026-06-29_e2e.md" "$TF"; then
  pass "touched_capture wrote the task path to <sid>.touched"
else
  fail "<sid>.touched missing the path: $(cat "$TF" 2>/dev/null)"
fi

# Stage 2 — Stop Round1 reminder (reads the produced .touched).
echo "[Stage 2] Stop Round1"
O1=$(stop)
echo "$O1" | grep -q '"decision": *"block"' && pass "Round1 block reminder" || fail "no Round1 block: $O1"
[ "$(sidlines "$TASK")" = "0" ] && pass "not yet bound (LLM step pending)" || fail "premature bind"

# Stage 3 — Stop Round2 backstop binds.
echo "[Stage 3] Stop Round2 → bind"
O2=$(stop)
[ "$(sidlines "$TASK")" = "1" ] && pass "Round2 bound [s:$SID8] end-to-end" || fail "not bound: $(sidlines "$TASK")"
echo "$O2" | grep -q "auto-bound: .*2026-06-29_e2e.md \[s:$SID8\]" \
  && pass "F5 auto-bound reported" || fail "no F5 auto-bound: $O2"

# Stage 4 — exec-binding: a task never captured, bound via a [tasks:] carry.
echo "[Stage 4] exec-binding via [tasks:] carry"
ET="$PDIR/tasks/1_in_progress/2026-06-29_exec.md"
mk "$ET"
O3=$(stop "[pj:$PROJ] [tasks: 2026-06-29_exec.md] produced the result off-task")
[ "$(sidlines "$ET")" = "1" ] && pass "exec owning task bound via [tasks:] e2e" || fail "exec not bound: $(sidlines "$ET")"
echo "$O3" | grep -q "auto-bound: .*2026-06-29_exec.md \[s:$SID8\]" \
  && pass "exec F5 auto-bound reported" || fail "no exec F5: $O3"

# Stage 5 — F7a membership containment: out-of-request sidecar entry is skipped.
echo "[Stage 5] capture membership containment (F7a)"
IN="$PDIR/tasks/1_in_progress/2026-06-29_inset.md";  mk "$IN"
OUT="$PDIR/tasks/1_in_progress/2026-06-29_outset.md"; mk "$OUT"   # exists, never touched
NOW=$(uv run python -c "import time;print(time.time())")
cat > "$BF" << EOF
{"reminded":{},"exec_tried":[],"capture":{"status":"requested","items":{"tasks":["2026-06-29_inset.md"],"notes":[]},"requested_ts":$NOW,"tried_notes":[],"tried_tasks":[]}}
EOF
cat > "$STATE/$SID.capture" << 'EOF'
{"confirmed":[{"task":"2026-06-29_inset.md","summary":"in-set change"},{"task":"2026-06-29_outset.md","summary":"OUT-OF-REQUEST change"}],"note_links":[],"proposals":[]}
EOF
O5=$(stop)
[ "$(sidlines "$IN")"  = "1" ] && pass "in-set task applied"         || fail "in-set not applied: $(sidlines "$IN")"
[ "$(sidlines "$OUT")" = "0" ] && pass "out-of-request task skipped" || fail "out-of-request bound: $(sidlines "$OUT")"
echo "$O5" | grep -q "membership-skip: 2026-06-29_outset.md" \
  && pass "F5 membership-skip reported" || fail "no membership-skip line: $O5"

echo ""
echo "=== Done ==="
