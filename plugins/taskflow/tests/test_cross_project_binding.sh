#!/usr/bin/env bash
# test_cross_project_binding.sh — T-D2-1..T-D2-3: cross-project touched
# resolution in the Stop hook (project-notes/specs/capture-detection-gaps.md
# §3 / D2).
#
# Before D2 the hook resolved every touched task against the SINGLE project
# named by `state['project']`: a write into another project of the same repo was
# either basename-matched into the wrong project or dropped in silence by the
# F-L3 boundary guard (the 2026-08-08 e4a7583c incident — state `project` = A,
# `.touched` lines pointing into project B, no `@log` line anywhere). D2 derives
# the project from each ledger LINE (`^_projects/([^/]+)/`), validates it by
# probing for a `tasks/` subdirectory, and keys every internal set by the
# QUALIFIED `<project>/<basename>`:
#
#   T-D2-1  a touched task outside `state['project']` is bound in ITS OWN
#           project root (e4a7583c fixture), alongside the primary project's
#           own task — and a note write in the other project reaches that
#           project's owning task through ITS reverse index
#   T-D2-1s `_projects/_state/...` lines match the extraction regex but are NOT
#           a project: they must never appear as a project root (real data:
#           7 such lines in one repo's ledgers, 6 in the other's). Same for a
#           directory under `_projects/` that carries no `tasks/`.
#   T-D2-2  F-4 migration (§3.4): a `.bind` written by W2/W3 holds BARE keys.
#           They are read as the PRIMARY project's qualified keys — the 打止め
#           still bites, the closed item set still backstops — and are written
#           back normalized, so bare keys disappear after one Stop.
#   T-D2-3  the F-L3 boundary guard works PER project: the same basename in two
#           projects binds the copy the ledger actually names, and a line naming
#           a project that does not exist binds nothing at all.
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted 250 real
# session-state files there). The real dir's file count is bracketed below.
#
# Usage:  bash plugins/taskflow/tests/test_cross_project_binding.sh
# Exit:   0 = all pass, 1 = failure
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJA="_test-d2a-$$"          # the session's project (state['project'])
PROJB="_test-d2b-$$"          # the OTHER project the ledger touches
ADIR="$PROJECTS/$PROJA"
BDIR="$PROJECTS/$PROJB"
NOPROJ="_test-d2-noproj-$$"   # a directory under _projects/ with no tasks/
SID="d2cross$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"; CF="$STATE/$SID.capture"

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

mkdir -p "$ADIR/tasks/1_in_progress" "$ADIR/project-notes/specs" \
         "$BDIR/tasks/1_in_progress" "$BDIR/project-notes/specs" \
         "$PROJECTS/$NOPROJ/project-notes" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJA" > "$SF"
printf '# index\n' > "$ADIR/project-notes/index.md"
printf '# index\n' > "$BDIR/project-notes/index.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {  # $1 = task md path (with an @log block, no @notes)
  cat > "$1" << 'T'
---
priority: HIGH
---

# Cross-project test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->
T
}

mk_linked() {  # $1 = task md path, $2 = note project-rel pre-linked in @notes
  cat > "$1" << T
---
priority: HIGH
---

# Cross-project note-owner task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->

<!-- @notes:begin -->
<!-- auto-managed by taskflow note-link; do not hand-edit -->
- $2
<!-- @notes:end -->
T
}

write_touched() {  # $1 = absolute path under $PROJECTS → append one ledger EVENT
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

write_touched_raw() {  # $1 = a literal repo-relative ledger line
  printf '%s\n' "$1" >> "$TF"
}

stop() {  # $1 = expiry seconds; $2 = optional last_assistant_message
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_LAM="${2:-}" TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

sidlines() {  # $1 = task md path → count [s:SID8] lines inside the @log block
  uv run --no-project python - "$1" "$SID8" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

bindq() {  # $1 = python expression over `c` (the .bind capture dict)
  uv run --no-project python - "$BF" "$1" << 'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except (OSError, ValueError):
    print("NOBIND"); raise SystemExit
c = (d.get("capture") or {})
print(eval(sys.argv[2]))
PY
}

echo "=== T-D2-1..3: cross-project touched resolution (capture-detection-gaps.md §3 / D2) ==="
echo "  state project=$PROJA  other project=$PROJB  sid8=$SID8  (isolated tempdir: $TMP)"
echo ""

# =====================================================================
# T-D2-1: the e4a7583c fixture — state `project` = A, the ledger writes into
# B. Pre-D2 the B task never received a line: `resolve_touched_tasks` indexed
# only A's tasks/, and even a basename hit was rejected by the F-L3 guard.
# =====================================================================
echo "[T-D2-1] a touched task outside state['project'] binds in ITS OWN project"
reset_state
TA="$ADIR/tasks/1_in_progress/2026-08-09_a-task.md"; mk "$TA"
TB="$BDIR/tasks/1_in_progress/2026-08-09_b-task.md"; mk "$TB"
NB="$BDIR/project-notes/specs/b-note.md"; printf '# b note\n' > "$NB"
NBREL="project-notes/specs/b-note.md"
TBN="$BDIR/tasks/1_in_progress/2026-08-09_b-note-owner.md"; mk_linked "$TBN" "$NBREL"
write_touched "$TA"
write_touched "$TB"
write_touched "$NB"          # a NOTE write in the OTHER project
O11=$(stop 999)
echo "$O11" | grep -q '"decision": *"block"' \
  && pass "the round forms across both projects" || fail "no round: $O11"
[ "$(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')" \
  = "$PROJA/2026-08-09_a-task.md,$PROJB/2026-08-09_b-note-owner.md,$PROJB/2026-08-09_b-task.md" ] \
  && pass "items.tasks holds all three tasks, each qualified by its OWN project" \
  || fail "items.tasks: $(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')"
echo "$O11" | grep -qF "$PROJB/2026-08-09_b-task.md" \
  && pass "the cross-project task is named (qualified) in the spawn context" \
  || fail "cross-project task missing from the context: $O11"
echo "$O11" | grep -qF "\\\"$PROJB\\\":" \
  && pass "the spawn context's project_roots carries the OTHER project's root" \
  || fail "project_roots missing $PROJB: $O11"

O12=$(stop 0)                # expiry -> the r1 backstop binds every item
[ "$(sidlines "$TB")" = "1" ] \
  && pass "T-D2-1: the cross-project task got its [s:$SID8] line (the e4a7583c loss)" \
  || fail "cross-project task not bound: $(sidlines "$TB")"
[ "$(sidlines "$TA")" = "1" ] \
  && pass "the primary project's own task is unaffected (still exactly one line)" \
  || fail "primary task lines: $(sidlines "$TA")"
[ "$(sidlines "$TBN")" = "1" ] \
  && pass "T-D2-1: the OTHER project's note owner was reached via ITS reverse index" \
  || fail "cross-project note owner not bound: $(sidlines "$TBN")"
grep -qF "[s:$SID8]: (auto) touched; summary pending (r1)" "$TB" \
  && pass "the cross-project line is the normal round-1 placeholder" \
  || fail "cross-project note wrong: $(grep -F "[s:$SID8]" "$TB")"

# =====================================================================
# T-D2-1s: `_projects/_state/...` matches `^_projects/([^/]+)/` but is the
# sidecar directory, not a project. Neither it nor a `_projects/<dir>` without
# a `tasks/` subdirectory may ever become a project root.
# =====================================================================
echo ""
echo "[T-D2-1s] _projects/_state/... and a tasks-less directory are NOT projects"
reset_state
TS="$ADIR/tasks/1_in_progress/2026-08-09_state-guard.md"; mk "$TS"
write_touched "$TS"
write_touched_raw "_projects/_state/$SID.touched"
write_touched_raw "_projects/_state/$SID.bind"
write_touched_raw "_projects/_state/tasks/1_in_progress/2026-08-09_state-guard.md"
write_touched_raw "_projects/$NOPROJ/project-notes/specs/x.md"
write_touched_raw "_projects/$NOPROJ/tasks/1_in_progress/2026-08-09_state-guard.md"
O1S=$(stop 999)
echo "$O1S" | grep -q '"decision": *"block"' \
  && pass "the round still forms from the real project's line" || fail "no round: $O1S"
echo "$O1S" | grep -qF "\\\"_state\\\":" \
  && fail "_state was accepted as a project root: $O1S" \
  || pass "T-D2-1s: _state never appears in project_roots"
echo "$O1S" | grep -qF "\\\"$NOPROJ\\\":" \
  && fail "a tasks-less _projects/ directory became a project root: $O1S" \
  || pass "T-D2-1s: a directory with no tasks/ never appears in project_roots"
[ "$(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')" \
  = "$PROJA/2026-08-09_state-guard.md" ] \
  && pass "T-D2-1s: only the real project's task entered the round" \
  || fail "items.tasks: $(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')"

# =====================================================================
# T-D2-2: F-4 (§3.4) — a `.bind` written by W2/W3 keys everything by BARE
# basename. Reading it must (a) still honour the 打止め, (b) still backstop the
# closed item set, and (c) write the keys back QUALIFIED so the bare form is
# gone after one Stop (no round lost, none replayed).
# =====================================================================
echo ""
echo "[T-D2-2] legacy BARE .bind keys are interpreted and normalized (F-4)"
reset_state
L1="$ADIR/tasks/1_in_progress/2026-08-09_legacy-item.md"; mk "$L1"
L2="$ADIR/tasks/1_in_progress/2026-08-09_legacy-tried.md"; mk "$L2"
write_touched "$L1"; write_touched "$L2"
OLD_TS=$(uv run --no-project python -c "import time;print(time.time()-3600)")
# Exactly the shape W2/W3 write: bare basenames in items.tasks / tried_tasks /
# log_seen / round_base, cursor already at the ledger end, round 1 in flight.
cat > "$BF" << EOF
{"reminded":{},"exec_tried":[],"capture":{"status":"requested","items":{"tasks":["2026-08-09_legacy-item.md","2026-08-09_legacy-tried.md"],"notes":[]},"requested_ts":$OLD_TS,"tried_notes":[],"tried_tasks":["2026-08-09_legacy-tried.md"],"touch_cursor":2,"round":1,"log_seen":{"2026-08-09_legacy-item.md":0},"round_base":{"2026-08-09_legacy-item.md":0}}}
EOF
O21=$(stop 0)                # expiry -> G backstop over the legacy closed set
[ "$(sidlines "$L1")" = "1" ] \
  && pass "T-D2-2: a bare items.tasks key still backstops (read as the primary project)" \
  || fail "legacy item not bound: $(sidlines "$L1")"
grep -qF "[s:$SID8]: (auto) touched; summary pending (r1)" "$L1" \
  && pass "T-D2-2: the legacy round keeps its own round tag (r1) — nothing replayed" \
  || fail "legacy placeholder wrong: $(grep -F "[s:$SID8]" "$L1")"
[ "$(sidlines "$L2")" = "0" ] \
  && pass "T-D2-2: a bare tried_tasks key still 打止め (no placeholder written)" \
  || fail "打止め lost across the migration: $(sidlines "$L2")"
[ "$(bindq '",".join(sorted(c.get("tried_tasks") or []))')" = "$PROJA/2026-08-09_legacy-tried.md" ] \
  && pass "T-D2-2: tried_tasks was written back QUALIFIED (bare key is gone)" \
  || fail "tried_tasks not normalized: $(bindq '",".join(sorted(c.get("tried_tasks") or []))')"
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJA/2026-08-09_legacy-item.md')")" = "1" ] \
  && pass "T-D2-2: log_seen was written back QUALIFIED (and F-1-resynced to 1)" \
  || fail "log_seen not normalized: $(bindq 'sorted((c.get("log_seen") or {}).items())')"
[ "$(bindq "'2026-08-09_legacy-item.md' in (c.get('log_seen') or {})")" = "False" ] \
  && pass "T-D2-2: no BARE key survives in the rewritten .bind" \
  || fail "a bare log_seen key survived: $(bindq 'sorted((c.get("log_seen") or {}).items())')"
[ "$(bindq 'c.get("round")')" = "1" ] \
  && pass "T-D2-2: the migration neither lost nor replayed a round (still r1)" \
  || fail "round: $(bindq 'c.get("round")')"

# =====================================================================
# T-D2-3: the F-L3 boundary guard applied PER project root. The same basename
# exists in both projects; only the copy the ledger NAMES may be bound. A line
# naming a project that does not exist binds nothing anywhere.
# =====================================================================
echo ""
echo "[T-D2-3] F-L3 per project: a colliding basename binds the copy the ledger names"
reset_state
DA="$ADIR/tasks/1_in_progress/2026-08-09_dup.md"; mk "$DA"
DB="$BDIR/tasks/1_in_progress/2026-08-09_dup.md"; mk "$DB"
write_touched "$DB"                                              # ONLY B's copy
write_touched_raw "_projects/_test-d2-ghost-$$/tasks/1_in_progress/2026-08-09_dup.md"
O31=$(stop 999)
[ "$(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')" \
  = "$PROJB/2026-08-09_dup.md" ] \
  && pass "T-D2-3: only the named project's copy entered the round" \
  || fail "items.tasks: $(bindq '",".join(sorted(((c.get("items") or {}).get("tasks") or [])))')"
stop 0 >/dev/null            # expiry -> backstop
[ "$(sidlines "$DB")" = "1" ] \
  && pass "T-D2-3: the ledger-named copy (project B) was bound" \
  || fail "B copy lines: $(sidlines "$DB")"
[ "$(sidlines "$DA")" = "0" ] \
  && pass "T-D2-3: the same-basename copy in project A was NOT bound" \
  || fail "A copy was wrongly bound: $(sidlines "$DA")"

echo ""
echo "[T-D2-3b] a ledger line naming a nonexistent project binds nothing"
reset_state
GA="$ADIR/tasks/1_in_progress/2026-08-09_ghost-only.md"; mk "$GA"
write_touched_raw "_projects/_test-d2-ghost-$$/tasks/1_in_progress/2026-08-09_ghost-only.md"
O32=$(stop 0)
if [ -z "$O32" ] || ! echo "$O32" | grep -q '"decision": *"block"'; then
  pass "T-D2-3b: no round forms from a line whose project does not exist"
else
  fail "a nonexistent project formed a round: $O32"
fi
[ "$(sidlines "$GA")" = "0" ] \
  && pass "T-D2-3b: the same-basename task in the real project was NOT bound" \
  || fail "ghost line bound a real task: $(sidlines "$GA")"

echo ""
echo "=== Done ==="
