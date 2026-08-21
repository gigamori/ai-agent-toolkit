#!/usr/bin/env bash
# test_selflog_placeholder_guard.sh — B-AC2: the NEGATIVE CONTROL for B-m1.
#
# 04-plan.md §2.4 B-AC2 (design:
# mode-orchestrator-runs/2026-08-12_capture-touched-tasks-empty-membership-skip/04-plan.md).
#
# What this pins
# --------------
# B-m1 splits the membership allow-set (`items.allow_tasks`, PRE self-log
# subtraction) away from `items.tasks` (POST subtraction). `items.tasks` is not
# only the old gate: per §0.2 U-1 it also drives the capture-expiry backstop
#
#     backstop = [(k, current_index.get(k)) for k in items['tasks']]
#
# and the `(referenced)` over-bind boundary `round_task_set`. So the failure mode
# B-m1 must avoid is widening `items.tasks` itself — B-m2's move ("drop the
# subtraction from `compute_round_active`"). Do that and a task the agent logged
# ITSELF re-enters the closed set, `round_base[key]` is seeded to the count that
# already includes the agent's own line, and at expiry the backstop's
# `count_sid_lines > _round_base` guard compares n > n, never fires, and staples
# `(auto) touched; summary pending` next to the line the agent just wrote.
#
# This script is that detector. Two arms differing in EXACTLY ONE variable —
# whether the agent wrote its own `[s:sid8]` line during the round:
#
#   B-AC2-DEFECT   task self-logged + a novel (unlinked) note opens the round.
#                  The task is in `items.allow_tasks` but NOT in `items.tasks`,
#                  and expiry appends NO placeholder to it.
#   B-AC2-CONTROL  byte-identical setup minus the self-log. The task IS in
#                  `items.tasks` and expiry DOES append the placeholder.
#
# The control arm is what makes the defect arm's "0 placeholders" mean anything:
# without a measured non-zero firing, "no placeholder" is equally consistent
# with "the guard works" and with "this fixture never reaches the backstop".
#
# Why the round is opened by a NOTE: with the task subtracted, `active` is empty,
# so the round must open on `novel_notes` (§1.6 `if ... (active or novel_notes)`)
# — this is 03-debug §4.3's OBS1-DEFECT shape. The note is deliberately UNLINKED
# (the task has no `@notes` block), so it has no reverse-index owner and the
# `(referenced)` over-bind cannot reach the task; the only thing that could write
# to it at expiry is the placeholder backstop under test.
#
# Placement (§5.2 row 3 left this to implementation): a dedicated script rather
# than an arm inside `test_round_binding.sh`. That file's header enumerates a
# closed spec contract (T-D1-1..T-D1-5 of capture-detection-gaps.md §1 / D1) and
# every arm is keyed to one of those IDs; B-AC2 belongs to a different design
# (04-plan (B)) and needs the rationale above in its own header. The other
# candidate venue — `test_touched_capture.sh`'s `[main]` section — was off
# limits under §5.1 and no longer exists at all: it was retired by the
# 2026-08-20 consolidation (mode-orchestrator-runs/
# 2026-08-20_test-touched-capture-sh-state-hazard/). That venue is now closed by
# non-existence, so the placement conclusion above is unchanged.
#
# State-dir sandbox (`e2e_state_dir_sandbox` -- cited by rule id: the rule file
# has moved once already and every candidate path is gitignored, so no path
# citation survives a clone): since the 2026-08-20 ancestor-walk rollout EVERY
# taskflow hook resolves its roots via `_find_state_root`, walking UP from the
# cwd (cwd included), so "cd into a tempdir" alone isolates nothing — with no
# nearer `_projects/_state` on the walk, a temp dir inside the repo tree
# resolves to the real one (2026-07-17 incident: a wrong-cwd run deleted 250
# real session-state files). What isolates this script is (a) the step-4 guards
# right after mktemp below — the temp dir exists, is outside the repo tree, and
# no ancestor of it holds `_projects/_state` — and (b) the fixture's own
# $TMP/_projects/_state, created before any hook runs, which stops the walk at
# the cwd itself. (b) is ordering-fragile: it holds only while every hook
# invocation happens after the fixture mkdir, so (a) is the load-bearing half —
# do NOT remove the guards on the argument that (b) suffices. The bulk sweep
# additionally targets a cwd-pinned SWEEP_STATE_DIR, but that pin covers the
# sweep alone, not the state-json / .bind / placeholder writes, so it is never
# the isolation argument. The real dir's file count is bracketed below.
#
# Usage:  bash plugins/taskflow/tests/test_selflog_placeholder_guard.sh
# Exit:   0 = all pass, 1 = failure, 2 = sandbox-guard abort (nothing ran)
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

TMP="$(mktemp -d)" || { echo "ABORT: mktemp -d failed" >&2; exit 2; }
# A failed mktemp would leave TMP empty, `cd ""` would fail silently under
# `set -uo pipefail` (no -e), the cwd would stay at $REPO_ROOT, and the guards
# below would pass an empty string — the exact shape they exist to stop. Check
# before guarding, not after.
[ -n "$TMP" ] && [ -d "$TMP" ] \
  || { echo "ABORT: mktemp -d yielded no usable dir ('$TMP')" >&2; exit 2; }
cd "$TMP" || { echo "ABORT: cd '$TMP' failed" >&2; rm -rf "$TMP"; exit 2; }

# --- e2e_state_dir_sandbox step 4: abort BEFORE any fixture exists ----------
# Exit 2, not 1: these are not test failures — nothing ran. Placed before the
# EXIT trap so an abort never enters the pass/fail tally. The ancestor walk
# below includes $TMP itself, which is correct here precisely because the
# fixture's own _projects/_state does not exist yet.
case "$TMP" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ABORT: temp workspace $TMP is INSIDE the repo tree ($REPO_ROOT);" \
         "the hooks' ancestor walk would resolve into the real _projects/_state." >&2
    cd /; rm -rf "$TMP"; exit 2 ;;
esac
d="$TMP"
while :; do
  if [ -d "$d/_projects/_state" ]; then
    echo "ABORT: ancestor $d of temp workspace holds _projects/_state;" \
         "the hooks' ancestor walk would reach it." >&2
    cd /; rm -rf "$TMP"; exit 2
  fi
  p="$(dirname "$d")"; [ "$p" = "$d" ] && break; d="$p"
done

PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJ="_test-selflog-$$"
PDIR="$PROJECTS/$PROJ"
SID="selflog$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"; CF="$STATE/$SID.capture"
PLACEHOLDER='(auto) touched; summary pending'

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
  # A detector that reports its failures only on stdout is not a detector for
  # anything that reads exit codes, so the EXIT trap sets the status itself.
  [ "$FAIL" -eq 0 ] || exit 1
}
trap cleanup EXIT

mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/specs" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {  # $1 = task md path — @log block, NO @notes block (so notes stay unlinked)
  cat > "$1" << 'T'
---
priority: HIGH
---

# Self-log placeholder guard task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-19: created
<!-- @log:end -->
T
}

write_touched() {  # $1 = absolute path under $PROJECTS → append one ledger EVENT
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

stop() {  # $1 = expiry seconds
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;sys.stdout.write(json.dumps({'session_id':os.environ['TASKFLOW_SID']}))" \
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

agent_log_line() {  # $1 = task md path, $2 = note — simulate the AGENT writing
  uv run --no-project python - "$1" "$SID8" "$2" << 'PY'
import sys
path, sid8, note = sys.argv[1], sys.argv[2], sys.argv[3]
c = open(path, encoding="utf-8").read()
at = c.index("<!-- @log:end -->")
line = "- 2026-08-19T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

echo "=== B-AC2: self-logged task gets NO expiry placeholder (04-plan §2.4) ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"

# =====================================================================
# B-AC2-DEFECT — 03-debug §4.3 OBS1-DEFECT shape: the agent logged the task
# itself, a novel note opens the round. B-m1 keeps the task OUT of
# `items.tasks` (backstop closed) while putting it IN `items.allow_tasks`
# (membership gate open). Widen `items.tasks` instead — B-m2's move — and the
# placeholder lands on top of the agent's own line.
# =====================================================================
echo ""
echo "[B-AC2-DEFECT] self-logged task: in allow_tasks, out of items.tasks, no placeholder"
reset_state
TD="$PDIR/tasks/1_in_progress/2026-08-19_selflog-defect.md"; mk "$TD"
ND="$PDIR/project-notes/specs/selflog-defect-note.md"; printf '# defect note\n' > "$ND"
write_touched "$TD"
write_touched "$ND"
agent_log_line "$TD" "agent wrote its own progress line"
OD1=$(stop 0)                # Stop#1: round 1 requested (opened by the novel note)

echo "$OD1" | grep -q '"decision": *"block"' \
  && pass "the round opened (capture requested) — the arm reaches the backstop path" \
  || fail "no capture was requested, so expiry never runs: $OD1"
[ "$(bindq 'c.get("round")')" = "1" ] \
  && pass "round 1 is open" || fail "round: $(bindq 'c.get("round")')"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "" ] \
  && pass "items.tasks is EMPTY — the self-logged task was subtracted (B-m1, not B-m2)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"
[ "$(bindq '",".join(((c.get("items") or {}).get("allow_tasks") or []))')" = "$PROJ/2026-08-19_selflog-defect.md" ] \
  && pass "items.allow_tasks DOES carry it — the membership gate stayed open" \
  || fail "items.allow_tasks: $(bindq '",".join(((c.get("items") or {}).get("allow_tasks") or []))')"
[ "$(bindq '",".join(((c.get("items") or {}).get("notes") or []))')" = "project-notes/specs/selflog-defect-note.md" ] \
  && pass "the novel note is what opened the round" \
  || fail "items.notes: $(bindq '",".join(((c.get("items") or {}).get("notes") or []))')"

OD2=$(stop 0)                # Stop#2: the request expires -> G backstop runs

[ "$(sidlines "$TD")" = "1" ] \
  && pass "still exactly ONE [s:sid8] line — the agent's own, nothing stapled to it" \
  || fail "line count changed at expiry: $(sidlines "$TD")"
if grep -qF "$PLACEHOLDER" "$TD"; then
  fail "PLACEHOLDER LEAKED onto a self-logged task: $(grep -F "$PLACEHOLDER" "$TD")"
else
  pass "no '$PLACEHOLDER' line on the self-logged task (B-AC2)"
fi
if echo "$OD2" | grep -qF "auto-bound"; then
  fail "expiry reported an auto-bind for a self-logged round: $OD2"
else
  pass "expiry reported no auto-bind at all"
fi

# =====================================================================
# B-AC2-CONTROL — identical fixture, self-log removed. This measures the
# NON-ZERO base rate: the backstop really does append the placeholder here, so
# the defect arm's zero is a property of the self-log, not of the fixture.
# =====================================================================
echo ""
echo "[B-AC2-CONTROL] same round without the self-log: the placeholder DOES fire"
reset_state
TC="$PDIR/tasks/1_in_progress/2026-08-19_selflog-control.md"; mk "$TC"
NC="$PDIR/project-notes/specs/selflog-control-note.md"; printf '# control note\n' > "$NC"
write_touched "$TC"
write_touched "$NC"
# (no agent_log_line — this is the ONLY difference from the defect arm)
OC1=$(stop 0)                # Stop#1: round 1 requested

echo "$OC1" | grep -q '"decision": *"block"' \
  && pass "the control round opened too (same fixture, same path)" \
  || fail "control round did not open: $OC1"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "$PROJ/2026-08-19_selflog-control.md" ] \
  && pass "items.tasks carries the un-logged task (nothing to subtract)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"
[ "$(sidlines "$TC")" = "0" ] \
  && pass "no line yet at request time" || fail "premature line: $(sidlines "$TC")"

OC2=$(stop 0)                # Stop#2: the request expires -> G backstop runs

[ "$(sidlines "$TC")" = "1" ] \
  && pass "expiry appended exactly one [s:sid8] line" \
  || fail "expiry line count: $(sidlines "$TC")"
if grep -qF "$PLACEHOLDER (r1)" "$TC"; then
  pass "the appended line IS '$PLACEHOLDER (r1)' — non-zero base rate measured"
else
  fail "control arm produced no placeholder; the defect arm's zero proves nothing"
fi
if echo "$OC2" | grep -qF "auto-bound"; then
  pass "expiry reported the auto-bind"
else
  fail "no auto-bind reported: $OC2"
fi

echo ""
echo "=== Done ==="
