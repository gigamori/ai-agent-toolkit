#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"

export TASKFLOW_CAPTURE_EXPIRY_S=0

HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
LOCK_HELPER="$REPO_ROOT/plugins/taskflow/hooks/log_lock.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS_DIR="$TMP/_projects"
STATE_DIR="$PROJECTS_DIR/_state"

PROJECT_NAME="_test-sid-gate-$$"
PROJECT_DIR="$PROJECTS_DIR/$PROJECT_NAME"
SID="sidbind$$-0000-0000-0000-000000000000"
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
  cd "$REPO_ROOT"
  rm -rf "$TMP"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT

echo "=== Test: sid-binding gate (PostToolUse .touched + exec-binding) ==="
echo "  project:  $PROJECT_DIR"
echo "  session:  $SID  (sid8=$SID8)"
echo "  isolated tempdir: $TMP"
  echo ""

mkdir -p "$PROJECT_DIR/tasks/0_todo" "$PROJECT_DIR/tasks/1_in_progress" \
         "$PROJECT_DIR/tasks/2_done" "$STATE_DIR"

make_task() {
  cat > "$1" << 'TASK'
---
priority: HIGH
created: 2026-06-26
updated: 2026-06-26
---

# Sid-binding gate test task

Incident-shaped task: created same session, @log line has no [s:] tag.

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-06-26: created
<!-- @log:end -->
TASK
}

make_task_no_log() {
  cat > "$1" << 'TASK'
---
priority: HIGH
created: 2026-06-26
updated: 2026-06-26
---

# No-log task

## Next Steps
- (none)
TASK
}

make_task_ambiguous_log() {
  cat > "$1" << 'TASK'
---
priority: HIGH
created: 2026-06-26
updated: 2026-06-26
---

# Ambiguous-log task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-06-26: created

<!-- @log:begin -->
- 2026-06-26: a hand edit duplicated the begin marker and ate the end marker
TASK
}

write_state() {
  local parent="${1:-}"
  if [ -n "$parent" ]; then
    cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"$PROJECT_NAME","parent_session_id":"$parent","timestamp":"2026-06-26T00:00:00"}
EOF
  else
    cat > "$STATE_FILE" << EOF
{"session_id":"$SID","project":"$PROJECT_NAME","timestamp":"2026-06-26T00:00:00"}
EOF
  fi
}

write_touched() {
  local rel="${1#$PROJECTS_DIR/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TOUCHED_FILE"
}

invoke_hook() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys; p={'session_id':os.environ['TASKFLOW_SID']}; lam=os.environ.get('TASKFLOW_LAM',''); p.update({'last_assistant_message':lam} if lam else {}); sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

invoke_hook_stderr() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys; p={'session_id':os.environ['TASKFLOW_SID']}; lam=os.environ.get('TASKFLOW_LAM',''); p.update({'last_assistant_message':lam} if lam else {}); sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$HOOK")" 2>&1 1>/dev/null
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

assert_block_intact() {
  uv run --no-project python - "$1" << 'PY'
import sys
content = open(sys.argv[1], encoding="utf-8").read()
b = content.count("<!-- @log:begin -->"); e = content.count("<!-- @log:end -->")
ib = content.find("<!-- @log:begin -->"); ie = content.find("<!-- @log:end -->")
print("INTACT" if (b == 1 and e == 1 and ib != -1 and ie != -1 and ib < ie) else "CORRUPT")
PY
}

echo "append_auto_binding unit: code-append guarantees [s:sid8]"
UNIT_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_ac1-unit.md"
make_task "$UNIT_TASK"
AC1A=$(uv run --no-project python - "$(to_win "$HOOK")" "$(to_win "$UNIT_TASK")" "$SID8" << 'PY'
import importlib.util, sys, os
hook_path, task, sid8 = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(hook_path))
spec = importlib.util.spec_from_file_location("cap", hook_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
before = mod.log_block_has_sid(task, sid8)
ok = mod.append_auto_binding(task, sid8, "2026-06-26T14:30:00")
after = mod.log_block_has_sid(task, sid8)
print("BEFORE=%s OK=%s AFTER=%s" % (before, ok, after))
PY
)
echo "$AC1A" | grep -q "BEFORE=False OK=True AFTER=True" \
  && pass "append_auto_binding adds [s:$SID8] when absent: $AC1A" \
  || fail "append_auto_binding did not guarantee [s:$SID8]: $AC1A"
grep -q "\[s:$SID8\]: (auto) touched; summary pending" "$UNIT_TASK" \
  && pass "auto line has the default '(auto) touched; summary pending' form" \
  || fail "auto line form wrong"
[ "$(assert_block_intact "$UNIT_TASK")" = "INTACT" ] \
  && pass "@log block intact after backstop append" || fail "@log block corrupted"

  echo ""
echo ".touched-driven Round1 -> Round2: NEXT Stop binds"
R2_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_ac1-round2.md"
make_task "$R2_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$R2_TASK"

OUT1=$(invoke_hook)
echo "$OUT1" | grep -q '"decision": *"block"' \
  && pass "Round1 emits decision:block reminder" \
  || fail "Round1 did not emit block reminder: $OUT1"
[ "$(count_sid_lines "$R2_TASK")" = "0" ] \
  && pass "after Round1, task still has 0 [s:] lines" || fail "unexpected [s:] after Round1"
[ -f "$BIND_FILE" ] && grep -q '"reminded"' "$BIND_FILE" \
  && pass ".bind sidecar records round state" || fail ".bind sidecar not written"

OUT2=$(invoke_hook)
[ "$(count_sid_lines "$R2_TASK")" = "1" ] \
  && pass "Round2 backstop auto-appended exactly one [s:$SID8] line" \
  || fail "Round2 did not bind: got $(count_sid_lines "$R2_TASK")"
echo "$OUT2" | grep -q "auto-bound: .*\[s:$SID8\]" \
  && pass "Round2 reports F5 auto-bound line" || fail "Round2 missing auto-bound F5: $OUT2"

  echo ""
echo "idempotency: existing [s:sid8] is not duplicate-appended"
invoke_hook >/dev/null
[ "$(count_sid_lines "$R2_TASK")" = "1" ] \
  && pass "re-running hook leaves exactly one [s:$SID8] line" \
  || fail "duplicate append: $(count_sid_lines "$R2_TASK")"

  echo ""
echo "[no-loop] touched task with un-repairable @log: bounded, no loop"
NL_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_no-log.md"
make_task_ambiguous_log "$NL_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$NL_TASK"

OUT_NL1=$(invoke_hook)
echo "$OUT_NL1" | grep -q '"decision": *"block"' \
  && pass "ambiguous-log task: Round1 reminder emitted once (allowed)" \
  || fail "ambiguous-log Round1 unexpected: $OUT_NL1"
OUT_NL2=$(invoke_hook)
echo "$OUT_NL2" | grep -q "bind-skip(no-anchor): .*2026-06-26_no-log.md" \
  && pass "ambiguous-log task: 2nd Stop reports bind-skip(no-anchor) once" \
  || fail "ambiguous-log 2nd Stop did not report the residue: $OUT_NL2"
[ "$(count_sid_lines "$NL_TASK")" = "0" ] \
  && pass "ambiguous-log task genuinely unbound (no @log block generated)" \
  || fail "ambiguous-log task was bound: $(count_sid_lines "$NL_TASK")"
OUT_NL3=$(invoke_hook)
if [ -z "$OUT_NL3" ] || ! echo "$OUT_NL3" | grep -q '"decision": *"block"'; then
  pass "ambiguous-log task: 3rd Stop does NOT block (stable no-loop)"
  else
  fail "ambiguous-log task LOOPED (3rd Stop blocked): $OUT_NL3"
  fi

  echo ""
echo "[no-stamp] untouched task gets no [s:]"
UNTOUCHED="$PROJECT_DIR/tasks/0_todo/2026-06-26_untouched.md"
make_task "$UNTOUCHED"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
OTHER="$PROJECT_DIR/tasks/0_todo/2026-06-26_other.md"; make_task "$OTHER"
write_touched "$OTHER"
invoke_hook >/dev/null
invoke_hook >/dev/null
[ "$(count_sid_lines "$UNTOUCHED")" = "0" ] \
  && pass "untouched task has 0 [s:$SID8] (no auto-stamp)" \
  || fail "untouched task was stamped: $(count_sid_lines "$UNTOUCHED")"
[ "$(count_sid_lines "$OTHER")" = "1" ] \
  && pass "touched OTHER task bound (control)" || fail "control OTHER not bound"

  echo ""
echo "[exec] [tasks:] carry binds owning task outside .touched"
EXEC_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_exec-owner.md"
make_task "$EXEC_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
OUT_EXEC=$(invoke_hook "[pj:$PROJECT_NAME] [tasks: 2026-06-26_exec-owner.md] did the work via reference")
[ "$(count_sid_lines "$EXEC_TASK")" = "1" ] \
  && pass "exec owning task bound via [tasks:] carry (not in .touched)" \
  || fail "exec-bind did not bind: $(count_sid_lines "$EXEC_TASK")"
echo "$OUT_EXEC" | grep -q "auto-bound: .*exec-owner.*\[s:$SID8\]" \
  && pass "exec-bind reports F5 auto-bound line" || fail "exec-bind missing F5: $OUT_EXEC"
grep -q "\[s:$SID8\]: (auto) executed via \[tasks:\] carry" "$EXEC_TASK" \
  && pass "exec-bind line carries the executed-via provenance note" \
  || fail "exec-bind provenance note wrong"
invoke_hook "[tasks: 2026-06-26_exec-owner.md]" >/dev/null
[ "$(count_sid_lines "$EXEC_TASK")" = "1" ] \
  && pass "exec-bind idempotent (one [s:] line)" || fail "exec-bind duplicated"

  echo ""
echo "[exec/fork] fork (parent_session_id) skips exec-binding"
FORK_EXEC="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_fork-exec.md"
make_task "$FORK_EXEC"
write_state "parentSID-1234"
rm -f "$BIND_FILE" "$TOUCHED_FILE"
invoke_hook "[tasks: 2026-06-26_fork-exec.md]" >/dev/null
[ "$(count_sid_lines "$FORK_EXEC")" = "0" ] \
  && pass "fork: exec owning task NOT bound (W2 delegation)" \
  || fail "fork exec-bind leaked: $(count_sid_lines "$FORK_EXEC")"
write_state

  echo ""
echo "[exec-skip] [tasks:] target with un-repairable @log → surfaced once, no loop"
SKIP_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_exec-noskip.md"
make_task_ambiguous_log "$SKIP_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
OUT_SK1=$(invoke_hook "[tasks: 2026-06-26_exec-noskip.md]")
echo "$OUT_SK1" | grep -q '"decision": *"block"' \
  && pass "exec-skip blocks once to report" || fail "exec-skip did not block: $OUT_SK1"
echo "$OUT_SK1" | grep -q "auto-skip(ambiguous): .*exec-noskip" \
 && pass "exec-skip surfaced in injection" || fail "exec-skip not surfaced: $OUT_SK1"
OUT_SK2=$(invoke_hook "[tasks: 2026-06-26_exec-noskip.md]")
if [ -z "$OUT_SK2" ] || ! echo "$OUT_SK2" | grep -q '"decision": *"block"'; then
  pass "exec-skip: 2nd Stop does NOT re-report (bounded by exec_tried, no loop)"
  else
  fail "exec-skip LOOPED (2nd Stop blocked): $OUT_SK2"
  fi

  echo ""
echo "[tolerant parse] torn trailing .touched line is dropped (no crash)"
TP_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_tolerant.md"
make_task "$TP_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$TP_TASK"
printf '_projects/%s/tasks/0_todo/2026-06-26_tor' "$PROJECT_NAME" >> "$TOUCHED_FILE"
OUT_TP1=$(invoke_hook 2>/dev/null); RC=$?
[ "$RC" = "0" ] && pass "hook exit 0 despite torn .touched line" || fail "hook crashed rc=$RC"
echo "$OUT_TP1" | grep -q '"decision": *"block"' \
  && pass "valid touched line still drives Round1 (torn line ignored)" \
  || fail "valid line not processed: $OUT_TP1"
invoke_hook >/dev/null
[ "$(count_sid_lines "$TP_TASK")" = "1" ] \
  && pass "valid task bound; torn-line target untouched" || fail "tolerant-parse bind wrong"

  echo ""
echo "[lock] log_lock self-check + concurrent append integrity"
LOCK_SELF=$(uv run --no-project python "$(to_win "$LOCK_HELPER")")
echo "$LOCK_SELF" | grep -q "acquired" && echo "$LOCK_SELF" | grep -q "released" \
  && pass "log_lock self-check acquires and releases" || fail "log_lock self-check: $LOCK_SELF"

LOCK_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_lock-race.md"
make_task "$LOCK_TASK"
appender() {
  uv run --no-project python - "$(to_win "$HOOK")" "$(to_win "$LOCK_TASK")" "$1" << 'PY'
import importlib.util, sys, os, time
hook_path, task, sid8 = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(hook_path))
spec = importlib.util.spec_from_file_location("cap", hook_path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
time.sleep(0.05)
mod.append_auto_binding(task, sid8, "2026-06-26T15:00:00")
PY
}
appender "aaaaaaaa" & P1=$!
appender "bbbbbbbb" & P2=$!
wait "$P1"; wait "$P2"
A_COUNT=$(uv run --no-project python - "$LOCK_TASK" << 'PY'
import re, sys
content = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", content, re.DOTALL)
block = m.group(1) if m else ""
print(block.count("[s:aaaaaaaa]") + block.count("[s:bbbbbbbb]"))
PY
)
[ "$A_COUNT" = "2" ] \
  && pass "both concurrent appends survived (no lost update): $A_COUNT" \
  || fail "concurrent append lost an update: got $A_COUNT (expected 2)"
[ "$(assert_block_intact "$LOCK_TASK")" = "INTACT" ] \
  && pass "@log block intact after concurrent appends" || fail "@log corrupted under concurrency"

  echo ""
echo "=== Done ==="
