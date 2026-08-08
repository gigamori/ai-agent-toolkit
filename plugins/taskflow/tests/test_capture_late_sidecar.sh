#!/usr/bin/env bash
# test_capture_late_sidecar.sh — T-W5-1..T-W5-4: a capture sidecar that arrives
# AFTER its round expired must still apply
# (project-notes/specs/capture-detection-gaps.md §1.9 / W5).
#
# W2 retired the round's closed item set once the backstop had run
# (`items = {'tasks': [], 'notes': []}`, `round_base = {}`). `_apply_capture`
# gates every `confirmed` / `note_links` entry on membership in that set, so a
# subagent slower than the 30s expiry had its ENTIRE judgment discarded —
# reported as `membership-skip`, one line per entry (observed live on session
# e810b706: two valid note links thrown away). The deterministic placeholder
# survived, so the task stayed bound; the rich summary and the note↔task links,
# which is everything the judgment layer produces, did not.
#
#   T-W5-1  the exact observed sequence: request a round with notes → expire it
#           → THEN write the sidecar → the next Stop APPLIES the summary and the
#           note links instead of membership-skipping them
#   T-W5-2  INV-1 proof: with `items` / `round_base` no longer retired, several
#           consecutive Stops past the expiry with no new activity stay SILENT
#           and add no duplicate `@log` line — for the placeholder backstop AND
#           for the `referenced` over-bind (the latter looped even WITH the
#           retirement, because it iterates the whole-session note scan and not
#           `items` at all — the retirement never bounded it)
#   T-W5-3  a task the agent logged itself this round gets no redundant
#           `(referenced)` line: `_round_base` falls back to the STOP-ENTRY
#           `log_seen` snapshot, not to the live dict the self-log pass has
#           already advanced to the current count
#   T-W5-4  the text-key bound, for the residual `round_base` cannot cover: an
#           owner the whole-session note scan carries into a LATER round, whose
#           repeat append is a no-op that must not be reported
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/. The real dir's file count is bracketed below.
#
# Usage:  bash plugins/taskflow/tests/test_capture_late_sidecar.sh
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
PROJ="_test-late-$$"
PDIR="$PROJECTS/$PROJ"
SID="latesid$$-0000-0000-0000-000000000000"
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

mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/procedures" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {  # $1 = task md path (with an @log block and an EMPTY @notes block)
  cat > "$1" << 'T'
---
priority: HIGH
---

# Late-sidecar test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->

<!-- @notes:begin -->
<!-- auto-managed by taskflow note-link; do not hand-edit -->
<!-- @notes:end -->
T
}

mk_linked() {  # $1 = task md path, $2 = note project-rel pre-linked in @notes
  cat > "$1" << T
---
priority: HIGH
---

# Late-sidecar note-owner task

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

agent_log_line() {  # $1 = task md path, $2 = note — simulate the AGENT writing
  uv run --no-project python - "$1" "$SID8" "$2" << 'PY'
import sys
path, sid8, note = sys.argv[1], sys.argv[2], sys.argv[3]
c = open(path, encoding="utf-8").read()
at = c.index("<!-- @log:end -->")
line = "- 2026-08-09T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

echo "=== T-W5-1..4: late capture sidecar (capture-detection-gaps.md §1.9 / W5) ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
echo ""

# =====================================================================
# T-W5-1: the exact live sequence — round with notes -> expiry Stop ->
# sidecar arrives LATE -> the next Stop must APPLY it.
# Pre-W5 every entry came back as `membership-skip` because the expiry
# Stop had retired `items` to the empty set.
# =====================================================================
echo "[T-W5-1] a sidecar delivered after the expiry Stop still applies"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-08-09_late.md"; mk "$T1"
N1="$PDIR/project-notes/procedures/late-a.md"; printf '# a\n' > "$N1"
N2="$PDIR/project-notes/procedures/late-b.md"; printf '# b\n' > "$N2"
N1REL="project-notes/procedures/late-a.md"
N2REL="project-notes/procedures/late-b.md"
write_touched "$T1"; write_touched "$N1"; write_touched "$N2"
stop 999 >/dev/null          # Stop#1: round 1 requested (tasks + 2 unlinked notes)
stop 0 >/dev/null            # Stop#2: expiry -> r1 placeholder, round resolved
[ "$(sidlines "$T1")" = "1" ] \
  && pass "the expiry Stop placeholder-bound the task (backstop intact)" \
  || fail "expiry placeholder count: $(sidlines "$T1")"
# The subagent finally finishes and writes its judgment.
cat > "$CF" << EOF
{"confirmed":[{"task":"2026-08-09_late.md","summary":"LATESUMMARY the round's real work"}],
 "note_links":[{"note":"$N1REL","task":"2026-08-09_late.md"},
               {"note":"$N2REL","task":"2026-08-09_late.md"}],
 "proposals":[]}
EOF
O13=$(stop 0)
echo "$O13" | grep -q "membership-skip" \
  && fail "the late sidecar was still membership-skipped: $O13" \
  || pass "no membership-skip: the retired-item-set regression is gone"
echo "$O13" | grep -q "applied summary: $PROJ/2026-08-09_late.md" \
  && pass "the late summary was applied and reported" || fail "no applied summary: $O13"
grep -qF "LATESUMMARY the round's real work" "$T1" \
  && pass "the rich summary text landed in the task @log" \
  || fail "summary text missing: $(grep -F "[s:$SID8]" "$T1")"
echo "$O13" | grep -qF "linked note: $N1REL -> $PROJ/2026-08-09_late.md" \
  && pass "note link 1 established and reported" || fail "link 1 missing: $O13"
echo "$O13" | grep -qF "linked note: $N2REL -> $PROJ/2026-08-09_late.md" \
  && pass "note link 2 established and reported" || fail "link 2 missing: $O13"
grep -qF -- "- $N1REL" "$T1" && grep -qF -- "- $N2REL" "$T1" \
  && pass "both note links are present in the task @notes block" \
  || fail "@notes block: $(sed -n '/@notes:begin/,/@notes:end/p' "$T1")"
[ "$(sidlines "$T1")" = "2" ] \
  && pass "placeholder + late summary = exactly 2 [s:$SID8] lines" \
  || fail "line count after late apply: $(sidlines "$T1")"
[ ! -e "$CF" ] \
  && pass "the late sidecar was consumed (unlinked)" || fail "sidecar not consumed"

# =====================================================================
# T-W5-2: INV-1 proof. `items` / `round_base` are no longer retired, so
# the placeholder backstop AND the `referenced` over-bind are re-entered
# for the same resolved round on every later Stop. Both must go silent.
# The `referenced` loop is the one that fired even WITH the retirement:
# it iterates the whole-session note scan, which `items` never bounded.
# =====================================================================
echo ""
echo "[T-W5-2] 4 consecutive Stops past the expiry: silent, no duplicate line"
reset_state
TA="$PDIR/tasks/1_in_progress/2026-08-09_inv1-plain.md"; mk "$TA"
NB="$PDIR/project-notes/procedures/inv1-owned.md"; printf '# owned\n' > "$NB"
NBREL="project-notes/procedures/inv1-owned.md"
TB="$PDIR/tasks/1_in_progress/2026-08-09_inv1-owner.md"; mk_linked "$TB" "$NBREL"
write_touched "$TA"; write_touched "$NB"
stop 999 >/dev/null          # Stop#1: round 1 requested (TA touched, TB via note)
O22=$(stop 0)                # Stop#2: expiry -> referenced (TB) + placeholder (TA)
echo "$O22" | grep -q "auto-bound" \
  && pass "the expiry Stop reported its deterministic binds" || fail "no expiry binds: $O22"
[ "$(sidlines "$TA")" = "1" ] \
  && pass "plain touched task got exactly one placeholder" || fail "TA lines: $(sidlines "$TA")"
[ "$(sidlines "$TB")" = "1" ] \
  && pass "note owner got exactly one (referenced) line" || fail "TB lines: $(sidlines "$TB")"
grep -qF "(referenced) owner of $NBREL via reverse-index; capture expired (r1)" "$TB" \
  && pass "the (referenced) line carries the reverse-index provenance" \
  || fail "TB note wrong: $(grep -F "[s:$SID8]" "$TB")"
LOOP_FAIL=0
for n in 3 4 5 6; do
  ON=$(stop 0)
  if [ -n "$ON" ]; then
    fail "Stop#$n past the expiry still blocked (INV-1 loop): $ON"
    LOOP_FAIL=1
  fi
done
[ "$LOOP_FAIL" = "0" ] \
  && pass "Stops #3-#6 past the expiry are completely silent (INV-1)" || true
[ "$(sidlines "$TA")" = "1" ] \
  && pass "no duplicate placeholder after 4 further Stops" || fail "TA lines: $(sidlines "$TA")"
[ "$(sidlines "$TB")" = "1" ] \
  && pass "no duplicate (referenced) line after 4 further Stops" || fail "TB lines: $(sidlines "$TB")"

# =====================================================================
# T-W5-3: the `_round_base` fallback. A note owner the agent logged ITSELF
# this round must not also get a `(referenced)` line. The owner is reached
# through the whole-session note scan and was never in this round's
# `round_base`, so the guard falls back — and the fallback must be the
# STOP-ENTRY `log_seen` snapshot. The live dict is useless here: the
# self-log pass has already raised it to the current count, so
# `count > log_seen` is false and the guard never fires.
# =====================================================================
echo ""
echo "[T-W5-3] a self-logged note owner gets no redundant (referenced) line"
reset_state
TC="$PDIR/tasks/1_in_progress/2026-08-09_r1-driver.md"; mk "$TC"
write_touched "$TC"
stop 999 >/dev/null          # round 1 opens on TC only ...
stop 0 >/dev/null            # ... and resolves: round_base is frozen on TC
ND="$PDIR/project-notes/procedures/selflog-owned.md"; printf '# d\n' > "$ND"
NDREL="project-notes/procedures/selflog-owned.md"
TD="$PDIR/tasks/1_in_progress/2026-08-09_selflog-owner.md"; mk_linked "$TD" "$NDREL"
write_touched "$ND"                                   # new activity: the note
agent_log_line "$TD" "the agent summarized this round itself"
O33=$(stop 0)
[ "$(sidlines "$TD")" = "1" ] \
  && pass "the self-logged owner carries exactly its OWN line" \
  || fail "redundant line(s): $(grep -F "[s:$SID8]" "$TD")"
grep -qF "(referenced) owner of" "$TD" \
  && fail "a redundant (referenced) line was written next to the agent's own" \
  || pass "no (referenced) line was written over the agent's own summary"
if [ -z "$O33" ]; then
  pass "the Stop is silent (nothing to report)"
else
  fail "the Stop reported something: $O33"
fi

# =====================================================================
# T-W5-4: the text-key bound, for the case `round_base` cannot cover. The
# `referenced` over-bind iterates the WHOLE-SESSION note scan (§1.9), so a
# LATER round reaches an owner that this round's `round_base` never froze.
# The fallback then compares the owner's count against itself and does not
# fire, so the over-bind writes an r{N}-tagged line for the new round —
# and from the Stop after that, the text key is what stops it: without the
# pre-check `append_auto_binding` no-ops but still returns True, the no-op
# is recorded in `auto_bound`, and the gate blocks on it every Stop for as
# long as `status` stays `expired`.
# =====================================================================
echo ""
echo "[T-W5-4] a note owner reached by a LATER round is text-key bounded"
reset_state
NE="$PDIR/project-notes/procedures/carry-owned.md"; printf '# e\n' > "$NE"
NEREL="project-notes/procedures/carry-owned.md"
TE="$PDIR/tasks/1_in_progress/2026-08-09_carry-owner.md"; mk_linked "$TE" "$NEREL"
TOTHER="$PDIR/tasks/1_in_progress/2026-08-09_carry-other.md"; mk "$TOTHER"
write_touched "$NE"
stop 999 >/dev/null          # r1 requested (TE via its note)
stop 0 >/dev/null            # r1 expiry -> (referenced) r1 on TE
[ "$(sidlines "$TE")" = "1" ] \
  && pass "r1 over-bound the note owner once" || fail "TE after r1: $(sidlines "$TE")"
write_touched "$TOTHER"      # a NEW round about a DIFFERENT task
stop 999 >/dev/null          # r2 requested -> round_base is now {TF}, TE is not in it
stop 0 >/dev/null            # r2 expiry -> placeholder TF + (referenced) r2 on TE
[ "$(sidlines "$TE")" = "2" ] \
  && pass "r2 adds one more (referenced) line (whole-session note scan, §1.9)" \
  || fail "TE after r2: $(grep -F "[s:$SID8]" "$TE")"
CARRY_FAIL=0
for n in 1 2 3; do
  ON=$(stop 0)
  if [ -n "$ON" ]; then
    fail "extra Stop #$n after r2 still blocked (text key did not bound it): $ON"
    CARRY_FAIL=1
  fi
done
[ "$CARRY_FAIL" = "0" ] \
  && pass "3 further Stops are silent — the text key bounds the carried owner" || true
[ "$(sidlines "$TE")" = "2" ] \
  && pass "no further (referenced) line accumulates on the carried owner" \
  || fail "TE line count drifted: $(sidlines "$TE")"

echo ""
echo "=== Done ==="
