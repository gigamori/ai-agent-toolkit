#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
export TASKFLOW_CAPTURE_EXPIRY_S=0
CAP="$REPO_ROOT/plugins/taskflow/hooks/touched_capture.py"
STOP="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJ="_e2e-cap-$$"
PDIR="$PROJECTS/$PROJ"
SID="e2ecap$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"

. "$REPO_ROOT/plugins/taskflow/tests/capture_paths.sh"

to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
cleanup() {
  if [ -e "$REAL_STATE_DIR/$SID.json" ] || [ -e "$REAL_STATE_DIR/$SID.touched" ] \
     || [ -e "$REAL_STATE_DIR/$SID.bind" ] || [ -e "$REAL_STATE_DIR/$SID.capture" ]; then
    fail "real _projects/_state/ was touched by this test run (session $SID leaked there)"
  else
    pass "real _projects/_state/ untouched (session $SID artifacts never created there)"
  fi
  cd "$REPO_ROOT"
  rm -rf "$TMP"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$PDIR/tasks/1_in_progress" "$STATE"

cat > "$SF" << EOF
{"session_id":"$SID","project":"$PROJ"}
EOF

mk() {
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

cap() {
  printf '{"session_id":"%s","tool_input":%s}' "$SID" "$1" | uv run --no-project python "$(to_win "$CAP")"
}
stop() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$STOP")"
}
sidlines() {
  uv run --no-project python - "$1" "$SID8" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

echo "=== E2E: touched_capture.py → <sid>.touched → session_progress_capture.py ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
  echo ""

TASK="$PDIR/tasks/1_in_progress/2026-06-29_e2e.md"
mk "$TASK"

echo "[Stage 1] touched_capture records the Write"
cap "{\"file_path\":\"$(to_win "$TASK")\"}"
if [ -f "$TF" ] && grep -q "_projects/$PROJ/tasks/1_in_progress/2026-06-29_e2e.md" "$TF"; then
  pass "touched_capture wrote the task path to <sid>.touched"
  else
  fail "<sid>.touched missing the path: $(cat "$TF" 2>/dev/null)"
  fi

echo "[Stage 2] Stop Round1"
O1=$(stop)
echo "$O1" | grep -q '"decision": *"block"' && pass "Round1 block reminder" || fail "no Round1 block: $O1"
[ "$(sidlines "$TASK")" = "0" ] && pass "not yet bound (LLM step pending)" || fail "premature bind"

EXP_SIDECAR="$(to_win "$STATE/$SID.r1.capture")"
EXP_PROJECT_ROOT="$(to_win "$PDIR")"
echo "$O1" | grep -qF "\\\"sidecar_path\\\":\\\"$EXP_SIDECAR\\\"" \
  && pass "context sidecar_path is absolute (matches STATE_DIR)" \
  || fail "context sidecar_path missing/wrong: $O1"
echo "$O1" | grep -qF "\\\"project_root\\\":\\\"$EXP_PROJECT_ROOT\\\"" \
  && pass "context project_root is absolute (matches project dir)" \
  || fail "context project_root missing/wrong: $O1"
echo "$O1" | grep -qF "\\\"project_roots\\\":{\\\"$PROJ\\\":\\\"$EXP_PROJECT_ROOT\\\"}" \
  && pass "D2: context carries project_roots {name: absolute root}" \
  || fail "context project_roots missing/wrong: $O1"
echo "$O1" | grep -qF "\\\"touched_tasks\\\":[\\\"$PROJ/2026-06-29_e2e.md\\\"]" \
  && pass "D2: context touched_tasks entries are qualified <project>/<basename>" \
  || fail "context touched_tasks not qualified: $O1"
echo "$O1" | grep -qF "\\\"project_root\\\":\\\"$EXP_PROJECT_ROOT\\\",\\\"project_roots\\\"" \
  && pass "D2: primary project_root is retained next to project_roots (compat)" \
  || fail "context lost the primary project_root: $O1"
echo "$O1" | grep -qF "\`$EXP_SIDECAR\` and write nothing else" \
  && pass "step-3 prose carries the SAME absolute sidecar_path as the context block" \
  || fail "step-3 prose sidecar path missing/mismatched: $O1"
echo "$O1" | grep -q '\"_projects/_state/' \
  && fail "relative sidecar_path literal (quoted form) leaked into context: $O1" \
  || pass "no quoted relative _projects/_state/ literal in context"
echo "$O1" | grep -q '`_projects/_state/' \
  && fail "relative sidecar_path literal (backtick form) leaked into prose: $O1" \
  || pass "no backtick relative _projects/_state/ literal in prose"

echo "[Stage 3] Stop Round2 → bind"
O2=$(stop)
[ "$(sidlines "$TASK")" = "1" ] && pass "Round2 bound [s:$SID8] end-to-end" || fail "not bound: $(sidlines "$TASK")"
echo "$O2" | grep -q "auto-bound: .*2026-06-29_e2e.md \[s:$SID8\]" \
  && pass "F5 auto-bound reported" || fail "no F5 auto-bound: $O2"

echo "[Stage 4] exec-binding via [tasks:] carry"
ET="$PDIR/tasks/1_in_progress/2026-06-29_exec.md"
mk "$ET"
O3=$(stop "[pj:$PROJ] [tasks: 2026-06-29_exec.md] produced the result off-task")
[ "$(sidlines "$ET")" = "1" ] && pass "exec owning task bound via [tasks:] e2e" || fail "exec not bound: $(sidlines "$ET")"
echo "$O3" | grep -q "auto-bound: .*2026-06-29_exec.md \[s:$SID8\]" \
  && pass "exec F5 auto-bound reported" || fail "no exec F5: $O3"

echo "[Stage 5] capture membership containment (F7a)"
IN="$PDIR/tasks/1_in_progress/2026-06-29_inset.md";  mk "$IN"
OUT="$PDIR/tasks/1_in_progress/2026-06-29_outset.md"; mk "$OUT"
NOW=$(uv run --no-project python -c "import time;print(time.time())")
cat > "$BF" << EOF
{"reminded":{},"exec_tried":[],"capture":{"status":"requested","items":{"tasks":["2026-06-29_inset.md"],"notes":[]},"requested_ts":$NOW,"tried_notes":[],"tried_tasks":[],"round":2}}
EOF
cat > "$(rcap 2)" << 'EOF'
{"confirmed":[{"task":"2026-06-29_inset.md","summary":"in-set change"},{"task":"2026-06-29_outset.md","summary":"OUT-OF-REQUEST change"}],"note_links":[],"proposals":[]}
EOF
O5=$(stop)
[ "$(sidlines "$IN")"  = "1" ] && pass "in-set task applied"         || fail "in-set not applied: $(sidlines "$IN")"
[ "$(sidlines "$OUT")" = "0" ] && pass "out-of-request task skipped" || fail "out-of-request bound: $(sidlines "$OUT")"
echo "$O5" | grep -q "membership-skip: $PROJ/2026-06-29_outset.md" \
  && pass "F5 membership-skip reported" || fail "no membership-skip line: $O5"

  echo ""
echo "=== Done ==="
