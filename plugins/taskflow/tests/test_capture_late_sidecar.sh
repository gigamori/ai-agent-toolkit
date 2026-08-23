#!/usr/bin/env bash

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

. "$REPO_ROOT/plugins/taskflow/tests/capture_paths.sh"

bind_get() {
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

mk() {
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

mk_linked() {
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

write_touched() {
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

stop() {
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;sys.stdout.write(json.dumps({'session_id':os.environ['TASKFLOW_SID']}))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

sidlines() {
  uv run --no-project python - "$1" "$SID8" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

agent_log_line() {
  uv run --no-project python - "$1" "$SID8" "$2" << 'PY'
import sys
path, sid8, note = sys.argv[1], sys.argv[2], sys.argv[3]
c = open(path, encoding="utf-8").read()
at = c.index("<!-- @log:end -->")
line = "- 2026-08-09T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

echo "=== late capture sidecar ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
  echo ""

echo "a sidecar delivered after the expiry Stop still applies"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-08-09_late.md"; mk "$T1"
N1="$PDIR/project-notes/procedures/late-a.md"; printf '# a\n' > "$N1"
N2="$PDIR/project-notes/procedures/late-b.md"; printf '# b\n' > "$N2"
N1REL="project-notes/procedures/late-a.md"
N2REL="project-notes/procedures/late-b.md"
write_touched "$T1"; write_touched "$N1"; write_touched "$N2"
  stop 999 >/dev/null
  stop 0   >/dev/null
[ "$(sidlines "$T1")" = "1" ] \
  && pass "the expiry Stop placeholder-bound the task (backstop intact)" \
  || fail "expiry placeholder count: $(sidlines "$T1")"
cat > "$(rcap 1)" << EOF
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
[ ! -e "$(rcap 1)" ] \
  && pass "the late sidecar was consumed (unlinked)" || fail "sidecar not consumed"

  echo ""
echo "4 consecutive Stops past the expiry: silent, no duplicate line"
reset_state
TA="$PDIR/tasks/1_in_progress/2026-08-09_inv1-plain.md"; mk "$TA"
NB="$PDIR/project-notes/procedures/inv1-owned.md"; printf '# owned\n' > "$NB"
NBREL="project-notes/procedures/inv1-owned.md"
TB="$PDIR/tasks/1_in_progress/2026-08-09_inv1-owner.md"; mk_linked "$TB" "$NBREL"
write_touched "$TA"; write_touched "$NB"
  stop 999 >/dev/null
O22=$(stop 0)
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
    fail "Stop#$n past the expiry still blocked: $ON"
    LOOP_FAIL=1
  fi
done
[ "$LOOP_FAIL" = "0" ] \
 && pass "Stops #3-#6 past the expiry are completely silent" || true
[ "$(sidlines "$TA")" = "1" ] \
  && pass "no duplicate placeholder after 4 further Stops" || fail "TA lines: $(sidlines "$TA")"
[ "$(sidlines "$TB")" = "1" ] \
  && pass "no duplicate (referenced) line after 4 further Stops" || fail "TB lines: $(sidlines "$TB")"

  echo ""
echo "a self-logged note owner gets no redundant (referenced) line"
reset_state
TC="$PDIR/tasks/1_in_progress/2026-08-09_r1-driver.md"; mk "$TC"
write_touched "$TC"
  stop 999 >/dev/null
  stop 0   >/dev/null
ND="$PDIR/project-notes/procedures/selflog-owned.md"; printf '# d\n' > "$ND"
NDREL="project-notes/procedures/selflog-owned.md"
TD="$PDIR/tasks/1_in_progress/2026-08-09_selflog-owner.md"; mk_linked "$TD" "$NDREL"
write_touched "$ND"
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

  echo ""
echo "the (referenced) over-bind is bounded by the round's closed item set"
reset_state
NE="$PDIR/project-notes/procedures/carry-owned.md"; printf '# e\n' > "$NE"
NEREL="project-notes/procedures/carry-owned.md"
TE="$PDIR/tasks/1_in_progress/2026-08-09_carry-owner.md"; mk_linked "$TE" "$NEREL"
TOTHER="$PDIR/tasks/1_in_progress/2026-08-09_carry-other.md"; mk "$TOTHER"
write_touched "$NE"
  stop 999 >/dev/null
  stop 0   >/dev/null
[ "$(sidlines "$TE")" = "1" ] \
  && pass "r1 over-bound the note owner once (owner IS in r1's items)" \
  || fail "TE after r1: $(sidlines "$TE")"
grep -qF "(referenced) owner of $NEREL via reverse-index; capture expired (r1)" "$TE" \
  && pass "the r1 line keeps the reverse-index provenance" \
  || fail "TE note wrong: $(grep -F "[s:$SID8]" "$TE")"
GROW_FAIL=0
for r in 2 3 4; do
  write_touched "$TOTHER"
  stop 999 >/dev/null
  stop 0   >/dev/null
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

  echo ""
echo "an owner inside a LATER round's closed set still gets its line"
write_touched "$TE"
  stop 999 >/dev/null
  stop 0   >/dev/null
[ "$(sidlines "$TE")" = "2" ] \
  && pass "r5 over-bound TE again (TE is in r5's items)" \
  || fail "TE after r5: $(grep -F "[s:$SID8]" "$TE")"
grep -qF "capture expired (r5)" "$TE" \
  && pass "the later round's line carries its own round tag" \
  || fail "no r5 line: $(grep -F "[s:$SID8]" "$TE")"

  echo ""
echo "a round-1 sidecar landing AFTER round 2 committed still applies"
reset_state
TR1="$PDIR/tasks/1_in_progress/2026-08-19_r1-main.md"; mk "$TR1"
NR1="$PDIR/project-notes/procedures/r1-main.md"; printf '# r1\n' > "$NR1"
NR1REL="project-notes/procedures/r1-main.md"
TR2="$PDIR/tasks/1_in_progress/2026-08-19_r1-next.md"; mk "$TR2"
write_touched "$TR1"; write_touched "$NR1"
  stop 999 >/dev/null
  stop 0   >/dev/null
write_touched "$TR2"
  stop 999 >/dev/null
[ "$(bind_get round)" = "2" ] \
  && pass "round 2 is the open round when the round-1 sidecar arrives" \
  || fail "round after Stop#3: $(bind_get round)"
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-main.md","summary":"R1LATE the round-1 judgement"}],
 "note_links":[{"note":"$NR1REL","task":"2026-08-19_r1-main.md"}],
 "proposals":[]}
EOF
OR11=$(stop 999)
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

  echo ""
echo "a sidecar outside the round history is discarded with one report"
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
 || pass "no re-report on the following Stop"

  echo ""
echo "two rounds' sidecars landing together: both apply, one transitions"
reset_state
TA3="$PDIR/tasks/1_in_progress/2026-08-19_r1-both-a.md"; mk "$TA3"
TB3="$PDIR/tasks/1_in_progress/2026-08-19_r1-both-b.md"; mk "$TB3"
write_touched "$TA3"
  stop 999 >/dev/null
  stop 0   >/dev/null
write_touched "$TB3"
  stop 999 >/dev/null
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

  echo ""
echo "the round history is pruned to the last K=3 rounds"
reset_state
for r in 1 2 3 4 5; do
  TK="$PDIR/tasks/1_in_progress/2026-08-19_r1-k$r.md"; mk "$TK"
  write_touched "$TK"
  stop 999 >/dev/null
  stop 0   >/dev/null
done
[ "$(bind_get round)" = "5" ] \
  && pass "five rounds were committed" || fail "round: $(bind_get round)"
[ "$(bind_get history_keys)" = "3,4,5" ] \
  && pass "history holds exactly rounds 3,4,5 (K=3)" \
  || fail "history keys: $(bind_get history_keys)"

  echo ""
echo "an old round's apply does not defer the current round's expiry"
reset_state
TA6="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-old.md"; mk "$TA6"
TB6="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-open.md"; mk "$TB6"
write_touched "$TA6"
  stop 999 >/dev/null
  stop 0   >/dev/null
write_touched "$TB6"
  stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-fa-old.md","summary":"FASUM the old round's judgement"}],
 "note_links":[],"proposals":[]}
EOF
OR16=$(stop 0)
grep -qF "FASUM the old round's judgement" "$TA6" \
  && pass "the old round's summary was applied" \
  || fail "old-round summary missing: $(grep -F "[s:$SID8]" "$TA6")"
[ "$(bind_get status)" = "expired" ] \
 && pass "the in-flight round still expired on schedule" \
  || fail "status after the old-round apply: $(bind_get status)"
grep -qF "(auto) touched; summary pending (r2)" "$TB6" \
  && pass "the current round's backstop ran on the same Stop" \
  || fail "no r2 placeholder: $(grep -F "[s:$SID8]" "$TB6")"
echo "$OR16" | grep -q "membership-skip" \
  && fail "the old-round apply was membership-skipped: $OR16" \
  || pass "no membership-skip on the old-round apply"

  echo ""
echo "an old round's apply does not suppress a SHARED task's placeholder"
reset_state
TS7="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-shared.md"; mk "$TS7"
write_touched "$TS7"
  stop 999 >/dev/null
  stop 0   >/dev/null
grep -qF "(auto) touched; summary pending (r1)" "$TS7" \
  && pass "setup: the shared task carries r1's placeholder" \
  || fail "setup broken, no r1 placeholder: $(grep -F "[s:$SID8]" "$TS7")"
write_touched "$TS7"
  stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-fa-shared.md","summary":"SHAREDSUM round 1's judgement"}],
 "note_links":[],"proposals":[]}
EOF
OR17=$(stop 0)
grep -qF "SHAREDSUM round 1's judgement" "$TS7" \
  && pass "the old round's summary was applied to the shared task" \
  || fail "old-round summary missing: $(grep -F "[s:$SID8]" "$TS7")"
grep -qF "(auto) touched; summary pending (r2)" "$TS7" \
  && pass "the shared task still got the CURRENT round's placeholder" \
  || fail "no r2 placeholder on the shared task: $(grep -F "[s:$SID8]" "$TS7")"
echo "$OR17" | grep -q "membership-skip" \
  && fail "the old-round apply was membership-skipped: $OR17" \
  || pass "no membership-skip on the shared-task old-round apply"

  echo ""
echo "the re-baseline absorbs the foreign apply only, not this round's self-log"
reset_state
TS7B="$PDIR/tasks/1_in_progress/2026-08-19_r1-fa-selflog.md"; mk "$TS7B"
write_touched "$TS7B"
  stop 999 >/dev/null
  stop 0   >/dev/null
write_touched "$TS7B"
  stop 999 >/dev/null
agent_log_line "$TS7B" "SELFLOG the agent's own round-2 line"
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-fa-selflog.md","summary":"SELFSUM round 1's judgement"}],
 "note_links":[],"proposals":[]}
EOF
OR17B=$(stop 0)
grep -qF "SELFSUM round 1's judgement" "$TS7B" \
  && pass "the old round's summary was applied alongside the self-log" \
  || fail "old-round summary missing: $(grep -F "[s:$SID8]" "$TS7B")"
grep -qF "(auto) touched; summary pending (r2)" "$TS7B" \
  && fail "spurious r2 placeholder despite the self-log: $(grep -F "[s:$SID8]" "$TS7B")" \
  || pass "the self-logged round got NO placeholder (delta re-baseline)"
echo "$OR17B" | grep -q "applied summary: $PROJ/2026-08-19_r1-fa-selflog.md (r1)" \
  && pass "the late apply's report line carries its own round tag (r1)" \
  || fail "no (r1) tag on the applied-summary report: $OR17B"

  echo ""
echo "an old round's sidecar cannot reach a task only the CURRENT round froze"
reset_state
TA8="$PDIR/tasks/1_in_progress/2026-08-19_r1-excl-a.md"; mk "$TA8"
TB8="$PDIR/tasks/1_in_progress/2026-08-19_r1-excl-b.md"; mk "$TB8"
write_touched "$TA8"
  stop 999 >/dev/null
  stop 0   >/dev/null
write_touched "$TB8"
  stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-excl-b.md","summary":"EXCLSUM must never land"}],
 "note_links":[],"proposals":[]}
EOF
OR18=$(stop 0)
echo "$OR18" | grep -q "membership-skip: $PROJ/2026-08-19_r1-excl-b.md" \
  && pass "the wrong-round entry was membership-skipped by name" \
  || fail "no membership-skip for B: $OR18"
grep -qF "EXCLSUM must never land" "$TB8" \
  && fail "the r1 sidecar reached a task only r2 froze: $(grep -F "[s:$SID8]" "$TB8")" \
  || pass "nothing from the r1 sidecar landed in B's @log"

  echo ""
echo "an r0 sidecar is discarded, not fail-open applied"
reset_state
TA9="$PDIR/tasks/1_in_progress/2026-08-19_r1-zero.md"; mk "$TA9"
cat > "$(rcap 0)" << EOF
{"confirmed":[{"task":"2026-08-19_r1-zero.md","summary":"ZEROSUM must never land"}],
 "note_links":[],"proposals":[]}
EOF
OR19=$(stop 999)
grep -qF "ZEROSUM must never land" "$TA9" \
  && fail "the r0 sidecar bypassed the gate: $(grep -F "[s:$SID8]" "$TA9")" \
  || pass "nothing from the r0 sidecar landed in the task @log"
echo "$OR19" | grep -q "round-mismatch: sidecar r0" \
  && pass "the r0 sidecar was reported as round-mismatch" \
  || fail "no round-mismatch report for r0: $OR19"
[ -e "$(rcap 0)" ] \
  && fail "the r0 sidecar was left on disk" \
  || pass "the r0 sidecar was consumed"

  echo ""
echo "a torn sidecar is retried in-window, disposed once out of window"
reset_state
TA10="$PDIR/tasks/1_in_progress/2026-08-19_r1-torn.md"; mk "$TA10"
write_touched "$TA10"
  stop 999 >/dev/null
printf '{"confirmed": [ {"task": TRUNCATED' > "$(rcap 1)"
O110=$(stop 0)
[ -e "$(rcap 1)" ] \
  && pass "in-window torn sidecar was left for retry (not consumed)" \
  || fail "in-window torn sidecar was consumed"
echo "$O110" | grep -q "round-mismatch: sidecar r1" \
  && fail "in-window torn sidecar was reported: $O110" \
  || pass "in-window torn sidecar stayed silent"
for _i in 2 3; do
write_touched "$TA10"
  stop 999 >/dev/null
  stop 0   >/dev/null
done
write_touched "$TA10"
  stop 999 >/dev/null
O111=$(stop 0)
[ -e "$(rcap 1)" ] \
  && fail "out-of-window torn sidecar still on disk" \
  || pass "out-of-window torn sidecar was consumed"
echo "$O111" | grep -q "round-mismatch: sidecar r1 unreadable and outside history" \
  && pass "the disposal was reported once as unreadable+outside" \
  || fail "no unreadable round-mismatch report: $O111"
O112=$(stop 0)
echo "$O112" | grep -q "round-mismatch: sidecar r1 unreadable" \
 && fail "the torn-sidecar report repeated: $O112" \
 || pass "no re-report on the following Stop"

  echo ""
echo "=== Done ==="
