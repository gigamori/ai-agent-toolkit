#!/usr/bin/env bash
# test_sid_binding_gate.sh — process-level tests for the PostToolUse-capture +
# exec-binding redesign of the sid-binding gate
# (spec _projects/harness-taskflow/project-notes/specs/exec-binding.md §8).
#
# Exercises the deterministic parts of the Stop hook
# plugins/taskflow/hooks/session_progress_capture.py and the shared bounded
# advisory lock plugins/taskflow/hooks/log_lock.py:
#   AC-1  Round1 -> Round2 backstop via the .touched ledger
#   AC-2  ledger idempotency
#   AC-2  no-loop: a touched task with no @log:end never blocks (INV-1)
#   AC-3  no-stamp: an untouched task is never auto-bound
#   AC-6  exec-binding: a [tasks:] carry binds an owning task not in .touched;
#         fork (parent_session_id) skips exec-binding
#   AC-7  log_lock serializes concurrent @log appends without corruption
#   AC-8  tolerant parse: a torn trailing .touched line is dropped, no crash
#
# These invoke the hook as a subprocess with a synthesized Stop payload (and a
# synthesized state + .touched + .bind sidecar). They do NOT rely on a live
# `claude` process, so each invocation re-imports the hook fresh from disk.
#
# The W1 git-diff (E / P2/P3) backstop and its AC-5 tests are RETIRED: detection
# is now PostToolUse `.touched` capture (exec-binding.md §3.2; git_changed_task_mds
# removed). Subagent writes (P3) are captured by touched_capture.py with the
# parent sid (TBD-1 probe 2026-06-28) and so flow through the same .touched path.
#
# Usage:  bash plugins/taskflow/tests/test_sid_binding_gate.sh
# Exit:   0 = all pass, 1 = failure
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$REPO_ROOT"

# note-task-link.md §10 option-a: the Round1 reminder is superseded by an async
# capture spawn-block, and the deterministic Round2 placeholder backstop now
# fires on capture EXPIRY rather than unconditionally on the 2nd Stop. Force
# immediate expiry so these process-level tests still exercise the 2-Stop
# request→backstop path without a 15s wall-clock wait (env hook in the Stop gate).
export TASKFLOW_CAPTURE_EXPIRY_S=0

HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
LOCK_HELPER="$REPO_ROOT/plugins/taskflow/hooks/log_lock.py"
PROJECTS_DIR="$REPO_ROOT/_projects"
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
  rm -rf "$PROJECT_DIR"
  rm -f "$STATE_FILE" "$BIND_FILE" "$TOUCHED_FILE"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT

echo "=== Test: sid-binding gate (PostToolUse .touched + exec-binding) ==="
echo "  project:  $PROJECT_DIR"
echo "  session:  $SID  (sid8=$SID8)"
echo ""

# ----------------------------------------------------------
# Fixtures
# ----------------------------------------------------------
mkdir -p "$PROJECT_DIR/tasks/0_todo" "$PROJECT_DIR/tasks/1_in_progress" \
         "$PROJECT_DIR/tasks/2_done" "$STATE_DIR"

# A task md whose @log block was written at creation WITHOUT a [s:] tag.
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

# A task md with NO @log block at all — Round2/exec bind cannot append; the gate
# must NOT loop on it (INV-1).
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

write_state() {
  # $1 = optional parent_session_id (presence => fork)
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

# Append a task md's repo-relative path to the <sid>.touched ledger (what
# touched_capture.py would write at runtime).
write_touched() {
  local rel="${1#$REPO_ROOT/}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TOUCHED_FILE"
}

# Feed a Stop payload on stdin (optional $1 = last_assistant_message); print the
# hook's stdout (block JSON, if any). The payload is built with python so the
# message is safely JSON-encoded.
invoke_hook() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run python -c "import json,os,sys; p={'session_id':os.environ['TASKFLOW_SID']}; lam=os.environ.get('TASKFLOW_LAM',''); p.update({'last_assistant_message':lam} if lam else {}); sys.stdout.write(json.dumps(p))" \
    | uv run python "$(to_win "$HOOK")"
}

invoke_hook_stderr() {
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run python -c "import json,os,sys; p={'session_id':os.environ['TASKFLOW_SID']}; lam=os.environ.get('TASKFLOW_LAM',''); p.update({'last_assistant_message':lam} if lam else {}); sys.stdout.write(json.dumps(p))" \
    | uv run python "$(to_win "$HOOK")" 2>&1 1>/dev/null
}

count_sid_lines() {
  uv run python - "$1" "$SID8" << 'PY'
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
  uv run python - "$1" << 'PY'
import sys
content = open(sys.argv[1], encoding="utf-8").read()
b = content.count("<!-- @log:begin -->"); e = content.count("<!-- @log:end -->")
ib = content.find("<!-- @log:begin -->"); ie = content.find("<!-- @log:end -->")
print("INTACT" if (b == 1 and e == 1 and ib != -1 and ie != -1 and ib < ie) else "CORRUPT")
PY
}

# =====================================================================
# AC-1a: append_auto_binding unit — code-append guarantees [s:sid8].
# =====================================================================
echo "[AC-1a] append_auto_binding unit: code-append guarantees [s:sid8]"
UNIT_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_ac1-unit.md"
make_task "$UNIT_TASK"
AC1A=$(uv run python - "$(to_win "$HOOK")" "$(to_win "$UNIT_TASK")" "$SID8" << 'PY'
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

# =====================================================================
# AC-1b: full-hook Round1 -> Round2 via .touched (LLM never appends).
# =====================================================================
echo ""
echo "[AC-1b] .touched-driven Round1 -> Round2: NEXT Stop binds"
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

# =====================================================================
# AC-2: ledger idempotency — existing [s:sid8] is not duplicate-appended.
# =====================================================================
echo ""
echo "[AC-2] idempotency: existing [s:sid8] is not duplicate-appended"
invoke_hook >/dev/null
[ "$(count_sid_lines "$R2_TASK")" = "1" ] \
  && pass "re-running hook leaves exactly one [s:$SID8] line" \
  || fail "duplicate append: $(count_sid_lines "$R2_TASK")"

# =====================================================================
# AC-2 (no-loop / INV-1): a touched task with NO @log:end never blocks.
# =====================================================================
echo ""
echo "[AC-2 no-loop] touched task without @log:end: gate must NOT block"
NL_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_no-log.md"
make_task_no_log "$NL_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$NL_TASK"

# Stop #1: missing + not reminded => Round1 block (one reminder is allowed).
OUT_NL1=$(invoke_hook)
echo "$OUT_NL1" | grep -q '"decision": *"block"' \
  && pass "no-log task: Round1 reminder emitted once (allowed)" \
  || fail "no-log Round1 unexpected: $OUT_NL1"
# Stop #2: reminded==1, Round2 append fails (no @log:end) => must exit 0, NO block.
OUT_NL2=$(invoke_hook)
if [ -z "$OUT_NL2" ] || ! echo "$OUT_NL2" | grep -q '"decision": *"block"'; then
  pass "no-log task: 2nd Stop does NOT block (no infinite loop)"
else
  fail "no-log task LOOPED (2nd Stop blocked): $OUT_NL2"
fi
# Stop #3: still no block (stable).
OUT_NL3=$(invoke_hook)
if [ -z "$OUT_NL3" ] || ! echo "$OUT_NL3" | grep -q '"decision": *"block"'; then
  pass "no-log task: 3rd Stop still does NOT block (stable no-loop)"
else
  fail "no-log task LOOPED (3rd Stop blocked): $OUT_NL3"
fi

# =====================================================================
# AC-3 (no-stamp): an untouched task is never auto-bound.
# =====================================================================
echo ""
echo "[AC-3 no-stamp] untouched task gets no [s:]"
UNTOUCHED="$PROJECT_DIR/tasks/0_todo/2026-06-26_untouched.md"
make_task "$UNTOUCHED"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
# Touch only R2_TASK-like other file; leave UNTOUCHED out of .touched.
OTHER="$PROJECT_DIR/tasks/0_todo/2026-06-26_other.md"; make_task "$OTHER"
write_touched "$OTHER"
invoke_hook >/dev/null   # Round1 for OTHER
invoke_hook >/dev/null   # Round2 binds OTHER
[ "$(count_sid_lines "$UNTOUCHED")" = "0" ] \
  && pass "untouched task has 0 [s:$SID8] (no auto-stamp)" \
  || fail "untouched task was stamped: $(count_sid_lines "$UNTOUCHED")"
[ "$(count_sid_lines "$OTHER")" = "1" ] \
  && pass "touched OTHER task bound (control)" || fail "control OTHER not bound"

# =====================================================================
# AC-6 (exec-binding): [tasks:] carry binds an owning task not in .touched;
# fork (parent_session_id) skips exec-binding.
# =====================================================================
echo ""
echo "[AC-6 exec] [tasks:] carry binds owning task outside .touched"
EXEC_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_exec-owner.md"
make_task "$EXEC_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"   # EXEC_TASK is NOT touched
OUT_EXEC=$(invoke_hook "[pj:$PROJECT_NAME] [tasks: 2026-06-26_exec-owner.md] did the work via reference")
[ "$(count_sid_lines "$EXEC_TASK")" = "1" ] \
  && pass "exec owning task bound via [tasks:] carry (not in .touched)" \
  || fail "exec-bind did not bind: $(count_sid_lines "$EXEC_TASK")"
echo "$OUT_EXEC" | grep -q "auto-bound: .*exec-owner.*\[s:$SID8\]" \
  && pass "exec-bind reports F5 auto-bound line" || fail "exec-bind missing F5: $OUT_EXEC"
grep -q "\[s:$SID8\]: (auto) executed via \[tasks:\] carry" "$EXEC_TASK" \
  && pass "exec-bind line carries the executed-via provenance note" \
  || fail "exec-bind provenance note wrong"
# Idempotent re-run.
invoke_hook "[tasks: 2026-06-26_exec-owner.md]" >/dev/null
[ "$(count_sid_lines "$EXEC_TASK")" = "1" ] \
  && pass "exec-bind idempotent (one [s:] line)" || fail "exec-bind duplicated"

echo ""
echo "[AC-6 exec/fork] fork (parent_session_id) skips exec-binding"
FORK_EXEC="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_fork-exec.md"
make_task "$FORK_EXEC"
write_state "parentSID-1234"          # fork
rm -f "$BIND_FILE" "$TOUCHED_FILE"
invoke_hook "[tasks: 2026-06-26_fork-exec.md]" >/dev/null
[ "$(count_sid_lines "$FORK_EXEC")" = "0" ] \
  && pass "fork: exec owning task NOT bound (W2 delegation)" \
  || fail "fork exec-bind leaked: $(count_sid_lines "$FORK_EXEC")"
write_state                           # restore non-fork for later tests

# =====================================================================
# AC-6 / F2 (exec-skip): a [tasks:] target with no @log:end is surfaced once
# (auto-skip in the injection; INV-1 c) and does NOT loop (bounded by exec_tried).
# =====================================================================
echo ""
echo "[AC-6/F2 exec-skip] [tasks:] target without @log:end → surfaced once, no loop"
SKIP_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_exec-noskip.md"
make_task_no_log "$SKIP_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
OUT_SK1=$(invoke_hook "[tasks: 2026-06-26_exec-noskip.md]")
echo "$OUT_SK1" | grep -q '"decision": *"block"' \
  && pass "exec-skip blocks once to report (INV-1 c)" || fail "exec-skip did not block: $OUT_SK1"
echo "$OUT_SK1" | grep -q "auto-skip(ambiguous): .*exec-noskip" \
  && pass "exec-skip surfaced in injection (AC-7)" || fail "exec-skip not surfaced: $OUT_SK1"
OUT_SK2=$(invoke_hook "[tasks: 2026-06-26_exec-noskip.md]")
if [ -z "$OUT_SK2" ] || ! echo "$OUT_SK2" | grep -q '"decision": *"block"'; then
  pass "exec-skip: 2nd Stop does NOT re-report (bounded by exec_tried, no loop)"
else
  fail "exec-skip LOOPED (2nd Stop blocked): $OUT_SK2"
fi

# =====================================================================
# AC-8 (tolerant parse): a torn trailing .touched line is dropped, no crash.
# =====================================================================
echo ""
echo "[AC-8 tolerant] torn trailing .touched line is dropped (no crash)"
TP_TASK="$PROJECT_DIR/tasks/0_todo/2026-06-26_tolerant.md"
make_task "$TP_TASK"
write_state
rm -f "$BIND_FILE" "$TOUCHED_FILE"
write_touched "$TP_TASK"                       # valid line (newline-terminated)
printf '_projects/%s/tasks/0_todo/2026-06-26_tor' "$PROJECT_NAME" >> "$TOUCHED_FILE"  # torn (no newline)
OUT_TP1=$(invoke_hook 2>/dev/null); RC=$?
[ "$RC" = "0" ] && pass "hook exit 0 despite torn .touched line" || fail "hook crashed rc=$RC"
echo "$OUT_TP1" | grep -q '"decision": *"block"' \
  && pass "valid touched line still drives Round1 (torn line ignored)" \
  || fail "valid line not processed: $OUT_TP1"
invoke_hook >/dev/null                         # Round2 binds the valid task
[ "$(count_sid_lines "$TP_TASK")" = "1" ] \
  && pass "valid task bound; torn-line target untouched" || fail "tolerant-parse bind wrong"

# =====================================================================
# AC-7 (lock): log_lock serializes concurrent @log appends without corruption.
# =====================================================================
echo ""
echo "[AC-7 lock] log_lock self-check + concurrent append integrity"
LOCK_SELF=$(uv run python "$(to_win "$LOCK_HELPER")")
echo "$LOCK_SELF" | grep -q "acquired" && echo "$LOCK_SELF" | grep -q "released" \
  && pass "log_lock self-check acquires and releases" || fail "log_lock self-check: $LOCK_SELF"

LOCK_TASK="$PROJECT_DIR/tasks/1_in_progress/2026-06-26_lock-race.md"
make_task "$LOCK_TASK"
appender() {
  uv run python - "$(to_win "$HOOK")" "$(to_win "$LOCK_TASK")" "$1" << 'PY'
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
A_COUNT=$(uv run python - "$LOCK_TASK" << 'PY'
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
