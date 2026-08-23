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
#            (through the per-round `{sid}.r2.capture` name — round 2, since
#            round 1 already closed via Stage 3's backstop)
#
# State-dir sandbox (`e2e_state_dir_sandbox`, project-notes/specs/
# capture-hook-sweep-sandbox.md): the Stop hook runs an unconditional stale-marker
# sweep on every invocation and resolves `_projects` via getcwd() (no env override).
# This test therefore `cd`s into an isolated tempdir and builds `_projects/` there —
# it NEVER cd's into $REPO_ROOT while invoking the hook, so the sweep can never
# reach the real _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted
# 250 real session-state files there).
#
# Usage:  bash plugins/taskflow/tests/test_e2e_capture_bind.sh
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# note-task-link.md §10 option-a: Round2 placeholder now backstops on capture
# expiry. Force immediate expiry so the capture-spawn request (Stage 2) is
# followed by the deterministic backstop bind (Stage 3) without a 30s wait.
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

# rcap(): the per-round sidecar path (R-1 D1). $STATE/$SID are already set above.
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
  printf '{"session_id":"%s","tool_input":%s}' "$SID" "$1" | uv run --no-project python "$(to_win "$CAP")"
}
stop() {  # $1 = optional last_assistant_message — invoke the real Stop hook
  TASKFLOW_LAM="${1:-}" TASKFLOW_SID="$SID" uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$STOP")"
}
sidlines() {  # $1 = task md path → count [s:SID8] inside @log block
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

# Stage 1 — capture a Write through the real PostToolUse hook.
echo "[Stage 1] touched_capture records the Write"
cap "{\"file_path\":\"$(to_win "$TASK")\"}"
if [ -f "$TF" ] && grep -q "_projects/$PROJ/tasks/1_in_progress/2026-06-29_e2e.md" "$TF"; then
  pass "touched_capture wrote the task path to <sid>.touched"
else
  fail "<sid>.touched missing the path: $(cat "$TF" 2>/dev/null)"
fi

# Stage 2 — Stop Round1 reminder (reads the produced .touched). This is also
# the capture-spawn block (§10.5), so it carries the context block +
# instructions checked below (project-notes/specs/capture-context-abs-path.md
# AC-1/AC-2/AC-6/AC-7).
echo "[Stage 2] Stop Round1"
O1=$(stop)
echo "$O1" | grep -q '"decision": *"block"' && pass "Round1 block reminder" || fail "no Round1 block: $O1"
[ "$(sidlines "$TASK")" = "0" ] && pass "not yet bound (LLM step pending)" || fail "premature bind"

# --- AC-1/AC-2/AC-6: context block + prose carry ABSOLUTE sidecar_path /
# project_root (never a relative "_projects/..." literal), and both agree on
# the same value. Expected values are derived at runtime via the existing
# to_win() helper (cygpath -m), never hardcoded (repo rule: no absolute local
# paths in tracked files).
# R-1 (capture-detection-gaps.md §4.4.1 D1): the sidecar name carries the round
# it belongs to (`{sid}.r{N}.capture`), so the path handed out here is round 1's
# — this Stop is the first request of the session.
EXP_SIDECAR="$(to_win "$STATE/$SID.r1.capture")"
EXP_PROJECT_ROOT="$(to_win "$PDIR")"
# NOTE: $O1 is itself JSON-encoded (`{"decision":"block","reason":"..."}`), so
# the embedded context-block quotes appear in $O1's literal text as
# BACKSLASH-escaped quotes (\" not "). Patterns below match that escaped form
# — an unescaped `"key":"..."` pattern would never match and any assertion
# built on it would silently false-pass regardless of the fix (caught while
# authoring this test: the naive pattern below matched the step-3 prose,
# which is embedded as plain backticks, no escaping — but not the quoted JSON
# field, which needed the backslash).
echo "$O1" | grep -qF "\\\"sidecar_path\\\":\\\"$EXP_SIDECAR\\\"" \
  && pass "AC-1: context sidecar_path is absolute (matches STATE_DIR)" \
  || fail "context sidecar_path missing/wrong: $O1"
echo "$O1" | grep -qF "\\\"project_root\\\":\\\"$EXP_PROJECT_ROOT\\\"" \
  && pass "AC-1: context project_root is absolute (matches project dir)" \
  || fail "context project_root missing/wrong: $O1"
# --- D2 (capture-detection-gaps.md §3.3): the context gained `project_roots`
# (name -> absolute root, so a task in a NON-primary project is resolvable) and
# `touched_tasks` entries are now QUALIFIED `<project>/<basename>`. Both are
# pinned here because they are the contract `agents/progress-capture.md` reads.
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
  && pass "AC-2: step-3 prose carries the SAME absolute sidecar_path as the context block" \
  || fail "step-3 prose sidecar path missing/mismatched: $O1"
# AC-6/AC-7 negative: no relative "_projects/_state/" literal survives, in
# EITHER quoted-JSON form (escaped-quote anchor) or backtick-prose form
# (F-R1: a single anchor only covers one of the two sites).
echo "$O1" | grep -q '\"_projects/_state/' \
  && fail "relative sidecar_path literal (quoted form) leaked into context: $O1" \
  || pass "AC-6: no quoted relative _projects/_state/ literal in context"
echo "$O1" | grep -q '`_projects/_state/' \
  && fail "relative sidecar_path literal (backtick form) leaked into prose: $O1" \
  || pass "AC-2/AC-6: no backtick relative _projects/_state/ literal in prose"

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
# Round 1 already closed via Stage 3's backstop (BF's capture.round == 1 with
# status "expired" at this point — verified: probing $BF right before this
# overwrite shows `"round": 1, ... "status": "expired"`, and no `[tasks:]`
# carry since has opened a new one). This synthetic request therefore opens
# round 2, and its judgment sidecar is delivered under the per-round name the
# hook actually reads (`{sid}.r2.capture`), not the legacy un-suffixed name.
echo "[Stage 5] capture membership containment (F7a)"
IN="$PDIR/tasks/1_in_progress/2026-06-29_inset.md";  mk "$IN"
OUT="$PDIR/tasks/1_in_progress/2026-06-29_outset.md"; mk "$OUT"   # exists, never touched
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
