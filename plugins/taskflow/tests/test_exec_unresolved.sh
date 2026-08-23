#!/usr/bin/env bash
# test_exec_unresolved.sh — F-5: an UNRESOLVABLE `[tasks:]` exec carry is no
# longer dropped in silence
# (project-notes/specs/review-2026-08-19-fixes.md §5, exec-binding.md AC-9).
#
# A `[tasks: foo.md]` carry naming no task md in the PRIMARY project resolved to
# nothing, so it never reached the exec-bind loop, never entered A_r, and — since
# execution-by-reference leaves no `.touched` line by construction — was picked
# up by nothing else: zero trace on every channel. This test pins the closure:
#
#   1. silent-loss closure: the miss is the ONLY reason the Stop blocks, it is on
#      stderr, and it appears exactly once in the block reason
#   2. 打止め record: the BARE basename lands in top-level `exec_tried` in `.bind`
#   3. boundedness (INV-1): later Stops with the same carry are silent
#   4. self-healing PRESERVED: once the named task file exists, the SAME carry
#      binds it — the fix bounds the REPORT, never the RESOLUTION (§5.2)
#   5. cross-project hint: a basename that exists in another RESOLVED project is
#      reported with `(exists in: <other>)`, and that task is still NOT bound
#      (resolution stays primary-only, §3.6)
#   6. fork guard: under `parent_session_id` there is no report and no block
#
# State-dir sandbox (`e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted 250 real
# session-state files there).
#
# Usage:  bash plugins/taskflow/tests/test_exec_unresolved.sh
# Exit:   0 = all pass, 1 = failure
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

# Capture the real state-dir count BEFORE anything else touches the filesystem.
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

# Force immediate capture expiry so the request → G-backstop path completes
# without a 30 s wall-clock wait (same hook the other suites use).
export TASKFLOW_CAPTURE_EXPIRY_S=0

HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS_DIR="$TMP/_projects"
STATE_DIR="$PROJECTS_DIR/_state"

PROJECT_NAME="_test-execunres-$$"
PROJECT_DIR="$PROJECTS_DIR/$PROJECT_NAME"
OTHER_NAME="_test-execother-$$"
OTHER_DIR="$PROJECTS_DIR/$OTHER_NAME"
SID="execunr$$-0000-0000-0000-000000000000"
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

echo "=== F-5: exec-skip(unresolved) — an unresolvable [tasks:] carry is reported ==="
echo "  project:  $PROJECT_DIR"
echo "  other:    $OTHER_DIR"
echo "  session:  $SID  (sid8=$SID8)"
echo "  isolated tempdir: $TMP"
echo ""

mkdir -p "$PROJECT_DIR/tasks/1_in_progress" "$OTHER_DIR/tasks/1_in_progress" "$STATE_DIR"

make_task() {
  cat > "$1" << 'TASK'
---
priority: HIGH
created: 2026-08-19
updated: 2026-08-19
---

# Exec-carry test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-19: created
<!-- @log:end -->
TASK
}

# $1 = optional parent_session_id (fork)
write_state() {
  if [ -n "${1:-}" ]; then
    cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"$PROJECT_NAME","parent_session_id":"$1","timestamp":"2026-08-19T00:00:00"}
EOF
  else
    cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"$PROJECT_NAME","timestamp":"2026-08-19T00:00:00"}
EOF
  fi
}

# Append a task md's repo-relative path to the <sid>.touched ledger (what
# touched_capture.py would write at runtime).
write_touched() {
  local rel="${1#$PROJECTS_DIR/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TOUCHED_FILE"
}

OUT_FILE="$TMP/out.txt"
ERR_FILE="$TMP/err.txt"
# Feed a Stop payload on stdin ($1 = last_assistant_message); stdout -> $OUT_FILE
# (echoed), stderr -> $ERR_FILE. The payload is built with python so the message
# is safely JSON-encoded.
invoke_hook() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys; p={'session_id':os.environ['TASKFLOW_SID']}; lam=os.environ.get('TASKFLOW_LAM',''); p.update({'last_assistant_message':lam} if lam else {}); sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$HOOK")" > "$OUT_FILE" 2> "$ERR_FILE"
  cat "$OUT_FILE"
}

count_sid_lines() {
  uv run --no-project python - "$1" "$SID8" << 'PY'
import re, sys
path, sid8 = sys.argv[1], sys.argv[2]
try:
    content = open(path, encoding="utf-8").read()
except OSError:
    print(-1); raise SystemExit
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", content, re.DOTALL)
block = m.group(1) if m else ""
print(block.count("[s:%s]" % sid8))
PY
}

exec_tried_of() {
  uv run --no-project python - "$BIND_FILE" << 'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    print("NOBIND"); raise SystemExit
print(",".join(d.get("exec_tried") or []))
PY
}

REAL_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-08-19_real.md"
make_task "$REAL_TASK"
TYPO_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-08-19_typo.md"
TYPO_BASE="2026-08-19_typo.md"
write_state

# =====================================================================
# Case 1 — silent-loss closure. `.touched` is deliberately EMPTY: nothing
# else can pick this work up, which is exactly what made the drop silent.
# =====================================================================
echo "[Case 1] unresolvable carry is reported (stderr + block reason)"
rm -f "$BIND_FILE" "$TOUCHED_FILE"
OUT1=$(invoke_hook "[pj:$PROJECT_NAME] [tasks: $TYPO_BASE] did the work by reference")
ERR1="$(cat "$ERR_FILE")"

echo "$OUT1" | grep -q '"decision": *"block"' \
  && pass "Stop#1 blocks (INV-1 c: a NEW exec-skip is reportable)" \
  || fail "Stop#1 did not block: $OUT1"
# The block must be the REPORT-only kind, not a spawn: `.touched` is empty and
# the carry resolved to nothing, so `active` and `novel_notes` are both empty.
# This pins the NEW gate term strictly (`exec_unresolved`), not a spawn that
# would have blocked anyway.
echo "$OUT1" | grep -q "Spawn the async capture subagent" \
  && fail "Stop#1 blocked to SPAWN, so the gate term is not pinned: $OUT1" \
  || pass "Stop#1 block is report-only (no spawn) — the unresolved carry IS the reason"

# NOTE: the message's em-dash is not encodable in the win32 console codepage, so
# Python's stderr backslashreplace handler mangles it. Assert the two ASCII
# halves around it rather than the raw character (test_bind_skip_no_anchor.sh
# documents the same hazard).
echo "$ERR1" | grep -qF "[progress capture] exec-skip(unresolved): $TYPO_BASE [s:$SID8]" \
  && pass "stderr carries the exec-skip(unresolved) line with basename + sid8" \
  || fail "stderr exec-skip line missing/wrong: $ERR1"
echo "$ERR1" | grep -qF "[tasks:] carry names no task md under _projects/$PROJECT_NAME/tasks/" \
  && pass "stderr line explains the cause (no task md under the project)" \
  || fail "stderr cause clause missing: $ERR1"

echo "$OUT1" | grep -qF "[progress capture] exec-skip(unresolved): $TYPO_BASE" \
  && pass "block reason carries the exec-skip(unresolved) F5 line" \
  || fail "block reason missing exec-skip line: $OUT1"
COUNT1=$(echo "$OUT1" | grep -oF "exec-skip(unresolved): $TYPO_BASE" | wc -l)
[ "$COUNT1" = "1" ] \
  && pass "block reason reports the basename exactly once (got $COUNT1)" \
  || fail "block reason reported the basename $COUNT1 times (expected 1)"
echo "$OUT1" | grep -qF "the carry resolves in the PRIMARY project only" \
  && pass "block reason carries the report-only explainer clause" \
  || fail "explainer clause missing: $OUT1"

[ "$(count_sid_lines "$REAL_TASK")" = "0" ] \
  && pass "no phantom bind: the real task got no [s:$SID8] line" \
  || fail "a line leaked into the real task: $(count_sid_lines "$REAL_TASK")"

# =====================================================================
# Case 2 — 打止め record: the BARE basename (no `_projects/` prefix) in the
# top-level `exec_tried` list of the `.bind` sidecar.
# =====================================================================
echo ""
echo "[Case 2] 打止め recorded as a bare basename in .bind exec_tried"
TRIED=$(exec_tried_of)
[ "$TRIED" = "$TYPO_BASE" ] \
  && pass ".bind exec_tried records the bare basename: $TRIED" \
  || fail ".bind exec_tried wrong: '$TRIED'"
case "$TRIED" in
  _projects/*) fail "exec_tried entry is path-shaped, not a bare basename: $TRIED" ;;
  *) pass "exec_tried entry has no _projects/ prefix (disjoint from the _rel() shape)" ;;
esac

# =====================================================================
# Case 3 — boundedness / INV-1: the same carry does not re-report.
# =====================================================================
echo ""
echo "[Case 3] boundedness: the same carry is not re-reported"
OUT2=$(invoke_hook "[tasks: $TYPO_BASE] still working by reference")
ERR2="$(cat "$ERR_FILE")"
if [ -z "$OUT2" ] || ! echo "$OUT2" | grep -q '"decision": *"block"'; then
  pass "Stop#2 does NOT block again (bounded by exec_tried, no loop)"
else
  fail "Stop#2 re-blocked (unbounded re-report): $OUT2"
fi
echo "$ERR2" | grep -q "exec-skip(unresolved)" \
  && fail "Stop#2 re-emitted the stderr exec-skip line: $ERR2" \
  || pass "Stop#2 does not re-emit the stderr exec-skip line"

OUT3=$(invoke_hook "[tasks: $TYPO_BASE] still working by reference")
ERR3="$(cat "$ERR_FILE")"
if [ -z "$OUT3" ] || ! echo "$OUT3" | grep -q '"decision": *"block"'; then
  pass "Stop#3 still silent (stable)"
else
  fail "Stop#3 blocked: $OUT3"
fi
echo "$ERR3" | grep -q "exec-skip(unresolved)" \
  && fail "Stop#3 re-emitted the stderr exec-skip line: $ERR3" \
  || pass "Stop#3 stderr still silent"

# =====================================================================
# Case 4 — self-healing PRESERVED (§5.2). The 打止め bounds the REPORT, never
# the RESOLUTION: once the claimed task file exists, the SAME durable carry
# binds it. This is the regression guard for "bound the report, not the fix".
# =====================================================================
echo ""
echo "[Case 4] self-healing: the carry still binds once the task file appears"
make_task "$TYPO_TASK"
OUT4=$(invoke_hook "[tasks: $TYPO_BASE] finally created the task")
ERR4="$(cat "$ERR_FILE")"
[ "$(count_sid_lines "$TYPO_TASK")" = "1" ] \
  && pass "the once-unresolvable carry bound the task after it was created" \
  || fail "resolution was suppressed: $(count_sid_lines "$TYPO_TASK")"
grep -q "\[s:$SID8\]: (auto) executed via \[tasks:\] carry" "$TYPO_TASK" \
  && pass "the bind carries the executed-via provenance note" \
  || fail "provenance note wrong in $TYPO_TASK"
echo "$OUT4" | grep -q "exec-skip(unresolved)" \
  && fail "exec-skip(unresolved) re-reported after the task appeared: $OUT4" \
  || pass "no exec-skip(unresolved) once the carry resolves"
echo "$ERR4" | grep -q "exec-skip(unresolved)" \
  && fail "stderr re-emitted exec-skip after resolution: $ERR4" \
  || pass "stderr carries no exec-skip once the carry resolves"

# =====================================================================
# Case 5 — cross-project hint. The basename exists ONLY in a second project,
# and that project is resolved because THIS session's ledger named a task in
# it. The report must say where it lives; resolution stays primary-only.
# =====================================================================
echo ""
echo "[Case 5] cross-project hint: (exists in: <other>), still not bound"
SHARED_BASE="2026-08-19_shared.md"
SHARED_TASK="$OTHER_DIR/tasks/1_in_progress/$SHARED_BASE"
make_task "$SHARED_TASK"
OTHER_TOUCHED="$OTHER_DIR/tasks/1_in_progress/2026-08-19_other-touched.md"
make_task "$OTHER_TOUCHED"
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$OTHER_TOUCHED"     # puts <other> into project_roots / current_index
OUT5=$(invoke_hook "[tasks: $SHARED_BASE] worked on the shared task by reference")
ERR5="$(cat "$ERR_FILE")"

echo "$ERR5" | grep -qF "[progress capture] exec-skip(unresolved): $SHARED_BASE [s:$SID8]" \
  && pass "stderr reports the cross-project basename as unresolved" \
  || fail "stderr exec-skip line missing for the shared basename: $ERR5"
echo "$ERR5" | grep -qF "(exists in: $OTHER_NAME)" \
  && pass "stderr line carries the best-effort (exists in: $OTHER_NAME) hint" \
  || fail "cross-project hint missing on stderr: $ERR5"
echo "$OUT5" | grep -qF "exec-skip(unresolved): $SHARED_BASE (exists in: $OTHER_NAME)" \
  && pass "block reason carries the basename with its hint" \
  || fail "block reason hint missing: $OUT5"
[ "$(count_sid_lines "$SHARED_TASK")" = "0" ] \
  && pass "the other project's task is NOT bound (resolution stays primary-only)" \
  || fail "cross-project bind leaked: $(count_sid_lines "$SHARED_TASK")"

# =====================================================================
# Case 6 — fork guard: under `parent_session_id` the whole exec path is
# delegated (W2), so an unresolvable carry produces no report and no block.
# =====================================================================
echo ""
echo "[Case 6] fork guard: no report, no block"
write_state "parentSID-1234"
rm -f "$BIND_FILE" "$TOUCHED_FILE"
FORK_BASE="2026-08-19_fork-typo.md"
OUT6=$(invoke_hook "[tasks: $FORK_BASE] fork did the work by reference")
ERR6="$(cat "$ERR_FILE")"
echo "$ERR6" | grep -q "exec-skip(unresolved)" \
  && fail "fork emitted an exec-skip(unresolved) line: $ERR6" \
  || pass "fork emits no exec-skip(unresolved) on stderr"
if [ -z "$OUT6" ] || ! echo "$OUT6" | grep -q '"decision": *"block"'; then
  pass "fork does NOT block on an unresolvable carry (W2 delegation)"
else
  fail "fork blocked: $OUT6"
fi
TRIED6=$(exec_tried_of)
[ "$TRIED6" = "" ] || [ "$TRIED6" = "NOBIND" ] \
  && pass "fork records no 打止め entry (exec_tried: '$TRIED6')" \
  || fail "fork wrote an exec_tried entry: '$TRIED6'"
write_state                        # restore non-fork state

echo ""
echo "=== Done ==="
