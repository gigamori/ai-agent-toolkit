#!/usr/bin/env bash
# test_capture_late_sidecar.sh — T-W5-1..T-W5-3 / T-W6-1..T-W6-2 / T-R1-1..T-R1-6:
# a capture sidecar that arrives AFTER its round expired must still apply
# (project-notes/specs/capture-detection-gaps.md §1.9 / W5, §4.4 / R-1).
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
#   T-W6-1  the round bound (W6, review-2026-08-19-fixes.md §1): the
#           `referenced` over-bind is scoped to the round's closed
#           `items['tasks']`. A LATER round's expiry still REACHES a carried
#           owner through the whole-session note scan, and neither the
#           round-tagged text key nor the `_round_base` fallback bounds it —
#           measured r1..r5 -> 5 false lines. This replaces T-W5-4, which
#           asserted that growth as correct behaviour (F-15: expectation rot)
#   T-W6-2  the POSITIVE arm: the bound scopes the over-bind, it does not
#           disable it. An owner that ENTERS a later round's closed set still
#           gets that round's line
#
# R-1 (§4.4): W5 only keeps the LAST round's closed set. Once the NEXT round
# commits, `items` is replaced, so a sidecar still in flight from the previous
# round was gated on the wrong set and had its whole judgment discarded as
# `membership-skip` (observed live three times in one session). The round now
# travels in the sidecar's FILE NAME (`{sid}.r{N}.capture`) and the `.bind`
# retains the last K=3 rounds' closed sets in `capture['history']`:
#
#   T-R1-1  the main regression: round N requested -> expires -> round N+1
#           commits -> only THEN does the r{N} sidecar land. It is applied
#           against ROUND N's closed set (no `membership-skip`) and round N+1's
#           status is NOT moved by it
#   T-R1-2  a sidecar naming a round outside the retained history is discarded
#           unapplied, reported exactly once as `round-mismatch`, and consumed
#           (no re-report on later Stops)
#   T-R1-3  r{N} and r{N+1} landing on the SAME Stop: both apply (ascending),
#           and only the current round transitions to `done`
#   T-R1-4  backward compatibility: a legacy un-suffixed `{sid}.capture` still
#           applies under the current round's items, exactly as before
#   T-R1-5  the history window is pruned to K=3 entries (`.bind` parsed directly)
#   T-R1-6  F-A: applying an OLD round's sidecar must not suppress the current
#           round's lifecycle — the in-flight round still expires on schedule
#           and its backstop still runs on that same Stop
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

reset_state() { rm -f "$TF" "$BF" "$CF" "$STATE/$SID".r*.capture; }

rcap() { echo "$STATE/$SID.r$1.capture"; }  # the per-round sidecar path (R-1 D1)

bind_get() {  # $1 = status | round | history_keys → read it out of `.bind`
  uv run --no-project python - "$BF" "$1" << 'PY'
import json, sys
try:
    cap = json.load(open(sys.argv[1], encoding="utf-8")).get("capture", {})
except (OSError, ValueError):
    cap = {}
key = sys.argv[2]
if key == "history_keys":
    print(",".join(sorted(cap.get("history", {}), key=int)))
else:
    print(cap.get(key, ""))
PY
}

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

echo "=== T-W5-1..3 / T-W6-1..2: late capture sidecar (capture-detection-gaps.md §1.9 / W5, §1.10 / W6) ==="
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
# T-W6-1: the (referenced) over-bind is bounded by the round's closed item set.
# The note SCAN is whole-session (§1.9), so a LATER round's expiry still REACHES
# an owner whose note that round never touched — the owner SET is what bounds it.
# Before W6 each expired round appended one more `(referenced) ... (rN)` line:
# the text key differs per round and `_round_base` falls back to a `log_seen`
# the F-1 resync already advanced, so neither guard fired (measured r1..r5 -> 5).
# =====================================================================
echo ""
echo "[T-W6-1] the (referenced) over-bind is bounded by the round's closed item set"
reset_state
NE="$PDIR/project-notes/procedures/carry-owned.md"; printf '# e\n' > "$NE"
NEREL="project-notes/procedures/carry-owned.md"
TE="$PDIR/tasks/1_in_progress/2026-08-09_carry-owner.md"; mk_linked "$TE" "$NEREL"
TOTHER="$PDIR/tasks/1_in_progress/2026-08-09_carry-other.md"; mk "$TOTHER"
write_touched "$NE"
stop 999 >/dev/null          # r1 requested (TE enters items via its note)
stop 0   >/dev/null          # r1 expiry -> (referenced) r1 on TE
[ "$(sidlines "$TE")" = "1" ] \
  && pass "r1 over-bound the note owner once (owner IS in r1's items)" \
  || fail "TE after r1: $(sidlines "$TE")"
grep -qF "(referenced) owner of $NEREL via reverse-index; capture expired (r1)" "$TE" \
  && pass "the r1 line keeps the reverse-index provenance" \
  || fail "TE note wrong: $(grep -F "[s:$SID8]" "$TE")"
GROW_FAIL=0
for r in 2 3 4; do
  write_touched "$TOTHER"    # a NEW round about a DIFFERENT task; the note is untouched
  stop 999 >/dev/null        # r$r requested -> items = {TOTHER}; TE is NOT in it
  stop 0   >/dev/null        # r$r expiry
  if [ "$(sidlines "$TE")" != "1" ]; then
    fail "r$r bound an owner for a note it never touched: $(grep -F "[s:$SID8]" "$TE")"
    GROW_FAIL=1
  fi
done
[ "$GROW_FAIL" = "0" ] \
  && pass "r2-r4 add no (referenced) line to an owner outside their closed set" || true
[ "$(sidlines "$TOTHER")" = "3" ] \
  && pass "each of r2-r4 still placeholder-bound its OWN task (backstop intact)" \
  || fail "TOTHER lines: $(sidlines "$TOTHER")"
for n in 1 2 3; do
  ON=$(stop 0); [ -n "$ON" ] && fail "Stop #$n after r4 still blocked: $ON"
done
pass "further Stops silent (same-round text-key bound retained)"

# =====================================================================
# T-W6-2: the POSITIVE arm — the bound must scope the over-bind, not disable it
# after r1. An owner that ENTERS a later round's closed set (its task md is
# written in that round's slice — which is exactly how a note link gets
# established in a later round) still gets that round's line.
# =====================================================================
echo ""
echo "[T-W6-2] an owner inside a LATER round's closed set still gets its line"
write_touched "$TE"
stop 999 >/dev/null          # r5 requested -> items includes TE
stop 0   >/dev/null          # r5 expiry -> (referenced) r5 on TE
[ "$(sidlines "$TE")" = "2" ] \
  && pass "r5 over-bound TE again (TE is in r5's items)" \
  || fail "TE after r5: $(grep -F "[s:$SID8]" "$TE")"
grep -qF "capture expired (r5)" "$TE" \
  && pass "the later round's line carries its own round tag" \
  || fail "no r5 line: $(grep -F "[s:$SID8]" "$TE")"

# =====================================================================
# T-R1-1: the R-1 regression. Round 1 is requested, expires, and round 2
# commits BEFORE the round-1 sidecar lands. The sidecar names round 1 in
# its file name, so it is applied against ROUND 1's frozen set — which is
# the only set its entries can be members of. Gated on the live `items`
# (round 2's) every entry came back as `membership-skip`.
# =====================================================================
echo ""
echo "[T-R1-1] a round-1 sidecar landing AFTER round 2 committed still applies"
reset_state
TR1="$PDIR/tasks/1_in_progress/2026-08-19_r1-main.md"; mk "$TR1"
NR1="$PDIR/project-notes/procedures/r1-main.md"; printf '# r1\n' > "$NR1"
NR1REL="project-notes/procedures/r1-main.md"
TR2="$PDIR/tasks/1_in_progress/2026-08-19_r1-next.md"; mk "$TR2"
write_touched "$TR1"; write_touched "$NR1"
stop 999 >/dev/null          # Stop#1: r1 requested (items = {TR1}, notes = {NR1})
stop 0   >/dev/null          # Stop#2: r1 expires -> placeholder
write_touched "$TR2"
stop 999 >/dev/null          # Stop#3: r2 requested -> `items` is now {TR2}
[ "$(bind_get round)" = "2" ] \
  && pass "round 2 is the open round when the round-1 sidecar arrives" \
  || fail "round after Stop#3: $(bind_get round)"
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-main.md","summary":"R1LATE the round-1 judgement"}],
 "note_links":[{"note":"$NR1REL","task":"2026-08-19_r1-main.md"}],
 "proposals":[]}
EOF
OR11=$(stop 999)             # Stop#4: r1 sidecar applies under r1's closed set
echo "$OR11" | grep -q "membership-skip" \
  && fail "the round-1 sidecar was membership-skipped against round 2's set: $OR11" \
  || pass "no membership-skip: the sidecar was gated on ITS OWN round"
echo "$OR11" | grep -q "applied summary: $PROJ/2026-08-19_r1-main.md" \
  && pass "the round-1 summary was applied and reported" || fail "no applied summary: $OR11"
grep -qF "R1LATE the round-1 judgement" "$TR1" \
  && pass "the round-1 summary text landed in the task @log" \
  || fail "summary text missing: $(grep -F "[s:$SID8]" "$TR1")"
echo "$OR11" | grep -qF "linked note: $NR1REL -> $PROJ/2026-08-19_r1-main.md" \
  && pass "the round-1 note link was established" || fail "link missing: $OR11"
[ ! -e "$(rcap 1)" ] \
  && pass "the round-1 sidecar was consumed (unlinked)" || fail "r1 sidecar not consumed"
[ "$(bind_get status)" = "pending" ] \
  && pass "round 2's request was NOT marked done by round 1's delivery" \
  || fail "status after the old-round apply: $(bind_get status)"

# =====================================================================
# T-R1-2: a sidecar whose round is outside the retained history is
# consumed WITHOUT being applied and reported exactly once
# (consume-then-report, F-C — a failed unlink must not re-report forever).
# =====================================================================
echo ""
echo "[T-R1-2] a sidecar outside the round history is discarded with one report"
cat > "$(rcap 99)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-main.md","summary":"R1BOGUS must never be applied"}],
 "note_links":[],"proposals":[]}
EOF
OR12=$(stop 999)
echo "$OR12" | grep -q "round-mismatch: sidecar r99 outside history (r1..r2); discarded" \
  && pass "the out-of-window sidecar was reported as round-mismatch with its span" \
  || fail "no round-mismatch report: $OR12"
[ "$(echo "$OR12" | grep -c "round-mismatch")" = "1" ] \
  && pass "reported exactly once" || fail "report count: $(echo "$OR12" | grep -c "round-mismatch")"
grep -qF "R1BOGUS must never be applied" "$TR1" \
  && fail "the discarded sidecar was applied anyway" \
  || pass "nothing from the discarded sidecar reached the task @log"
[ ! -e "$(rcap 99)" ] \
  && pass "the out-of-window sidecar was consumed" || fail "r99 sidecar not consumed"
OR12B=$(stop 999)
echo "$OR12B" | grep -q "round-mismatch" \
  && fail "the discard was re-reported on the next Stop: $OR12B" \
  || pass "no re-report on the following Stop (INV-1)"

# =====================================================================
# T-R1-3: r{N} and r{N+1} land on the SAME Stop. Both apply, oldest first,
# and only the CURRENT round transitions to `done` (D3 rows 1/2).
# =====================================================================
echo ""
echo "[T-R1-3] two rounds' sidecars landing together: both apply, one transitions"
reset_state
TA3="$PDIR/tasks/1_in_progress/2026-08-19_r1-both-a.md"; mk "$TA3"
TB3="$PDIR/tasks/1_in_progress/2026-08-19_r1-both-b.md"; mk "$TB3"
write_touched "$TA3"
stop 999 >/dev/null          # r1 requested: items = {TA3}
stop 0   >/dev/null          # r1 expires
write_touched "$TB3"
stop 999 >/dev/null          # r2 requested: items = {TB3}
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-both-a.md","summary":"BOTHSUMA round one"}],
 "note_links":[],"proposals":[]}
EOF
cat > "$(rcap 2)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-both-b.md","summary":"BOTHSUMB round two"}],
 "note_links":[],"proposals":[]}
EOF
OR13=$(stop 999)
echo "$OR13" | grep -q "membership-skip" \
  && fail "a same-Stop delivery was membership-skipped: $OR13" \
  || pass "neither sidecar was membership-skipped"
grep -qF "BOTHSUMA round one" "$TA3" \
  && pass "round 1's summary landed on its own task" \
  || fail "r1 summary missing: $(grep -F "[s:$SID8]" "$TA3")"
grep -qF "BOTHSUMB round two" "$TB3" \
  && pass "round 2's summary landed on its own task" \
  || fail "r2 summary missing: $(grep -F "[s:$SID8]" "$TB3")"
[ "$(bind_get status)" = "done" ] \
  && pass "the CURRENT round transitioned to done" || fail "status: $(bind_get status)"
[ ! -e "$(rcap 1)" ] && [ ! -e "$(rcap 2)" ] \
  && pass "both sidecars were consumed" || fail "a sidecar survived the apply"

# =====================================================================
# T-R1-4: backward compatibility — a legacy un-suffixed `{sid}.capture`
# still applies under the current round's items, unchanged.
# =====================================================================
echo ""
echo "[T-R1-4] a legacy un-suffixed sidecar still applies (compat branch)"
reset_state
TL4="$PDIR/tasks/1_in_progress/2026-08-19_r1-legacy.md"; mk "$TL4"
write_touched "$TL4"
stop 999 >/dev/null          # r1 requested: items = {TL4}
cat > "$CF" << EOF
{"confirmed":[{"task":"2026-08-19_r1-legacy.md","summary":"LEGACYSUM old-style sidecar"}],
 "note_links":[],"proposals":[]}
EOF
OR14=$(stop 999)
echo "$OR14" | grep -q "membership-skip" \
  && fail "the legacy sidecar was membership-skipped: $OR14" \
  || pass "the legacy sidecar passed the membership gate"
grep -qF "LEGACYSUM old-style sidecar" "$TL4" \
  && pass "the legacy sidecar's summary landed in the task @log" \
  || fail "legacy summary missing: $(grep -F "[s:$SID8]" "$TL4")"
[ "$(bind_get status)" = "done" ] \
  && pass "the legacy apply still transitions the round to done" \
  || fail "status: $(bind_get status)"
[ ! -e "$CF" ] && pass "the legacy sidecar was consumed" || fail "legacy sidecar not consumed"

# =====================================================================
# T-R1-5: the retained window is K=3 rounds. Five rounds must leave the
# LAST THREE in `capture['history']` — read straight out of `.bind`.
# =====================================================================
echo ""
echo "[T-R1-5] the round history is pruned to the last K=3 rounds"
reset_state
for r in 1 2 3 4 5; do
  TK="$PDIR/tasks/1_in_progress/2026-08-19_r1-k$r.md"; mk "$TK"
  write_touched "$TK"
  stop 999 >/dev/null        # round $r requested
  stop 0   >/dev/null        # round $r expires (placeholder), so the next opens
done
[ "$(bind_get round)" = "5" ] \
  && pass "five rounds were committed" || fail "round: $(bind_get round)"
[ "$(bind_get history_keys)" = "3,4,5" ] \
  && pass "history holds exactly rounds 3,4,5 (K=3)" \
  || fail "history keys: $(bind_get history_keys)"

# =====================================================================
# T-R1-6 (F-A): applying an OLD round's sidecar must not stand in for the
# CURRENT round's delivery. `applied_this_stop` is current-round-only, so
# the in-flight round still expires on this same Stop and its backstop
# still runs — otherwise every late arrival pushes the expiry clock out by
# one Stop.
# =====================================================================
echo ""
echo "[T-R1-6] an old round's apply does not defer the current round's expiry"
reset_state
TA6="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-old.md"; mk "$TA6"
TB6="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-open.md"; mk "$TB6"
write_touched "$TA6"
stop 999 >/dev/null          # r1 requested: items = {TA6}
stop 0   >/dev/null          # r1 expires
write_touched "$TB6"
stop 999 >/dev/null          # r2 requested: items = {TB6}, in flight
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-fa-old.md","summary":"FASUM the old round's judgement"}],
 "note_links":[],"proposals":[]}
EOF
OR16=$(stop 0)               # r1 applies AND r2 must expire on this same Stop
grep -qF "FASUM the old round's judgement" "$TA6" \
  && pass "the old round's summary was applied" \
  || fail "old-round summary missing: $(grep -F "[s:$SID8]" "$TA6")"
[ "$(bind_get status)" = "expired" ] \
  && pass "the in-flight round still expired on schedule (F-A)" \
  || fail "status after the old-round apply: $(bind_get status)"
grep -qF "(auto) touched; summary pending (r2)" "$TB6" \
  && pass "the current round's backstop ran on the same Stop" \
  || fail "no r2 placeholder: $(grep -F "[s:$SID8]" "$TB6")"
echo "$OR16" | grep -q "membership-skip" \
  && fail "the old-round apply was membership-skipped: $OR16" \
  || pass "no membership-skip on the old-round apply"

echo ""
echo "=== Done ==="
