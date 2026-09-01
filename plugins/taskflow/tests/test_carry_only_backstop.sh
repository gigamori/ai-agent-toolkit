#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

TMP="$(mktemp -d)" || { echo "ABORT: mktemp -d failed" >&2; exit 2; }
[ -n "$TMP" ] && [ -d "$TMP" ] \
  || { echo "ABORT: mktemp -d yielded no usable dir ('$TMP')" >&2; exit 2; }
cd "$TMP" || { echo "ABORT: cd '$TMP' failed" >&2; rm -rf "$TMP"; exit 2; }

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
PROJ="_test-carryonly"
PDIR="$PROJECTS/$PROJ"
SID="c0a11f00-7777-4777-8777-777777777777"
SID_CLEAN="${SID//-/}"
SID_TAG="${SID_CLEAN: -12}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"

cleanup() {
  LEAKED=""
  for f in "$REAL_STATE_DIR/$SID".*; do
    [ -e "$f" ] && LEAKED="$LEAKED $(basename "$f")"
  done
  if [ -z "$LEAKED" ]; then
    pass "real _projects/_state/ holds no artifact of session $SID (full 36-char probe)"
  else
    fail "synthetic session leaked into the real _projects/_state/:$LEAKED"
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

. "$REPO_ROOT/plugins/taskflow/tests/capture_paths.sh"

reset_state() { rm -f "$TF" "$BF" "$STATE/$SID".r*.capture; }

mk() {
  cat > "$1" << 'T'
---
priority: HIGH
---

# Carry-only backstop test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-09-02: created
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

# Carry-only note-owner task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-09-02: created
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
  TASKFLOW_SID="$SID" TASKFLOW_CARRY="${2:-}" \
    uv run --no-project python -c "import json,os,sys;c=os.environ.get('TASKFLOW_CARRY','');d={'session_id':os.environ['TASKFLOW_SID']};d['last_assistant_message']=('[tasks: %s]\nplan presented; nothing written.' % c) if c else 'no carry.';sys.stdout.write(json.dumps(d))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

count_note() {
  grep -cF "$2" "$1" 2>/dev/null || true
}

echo "=== carry-only backstop ==="
echo "  project=$PROJ  tag=$SID_TAG  (isolated tempdir: $TMP)"
echo ""

echo "a round opened SOLELY by a [tasks:] carry"
reset_state
TC1="$PDIR/tasks/1_in_progress/2026-09-02_carry-only.md"; mk "$TC1"
O11=$(stop 999 "2026-09-02_carry-only.md")
O12=$(stop 999 "2026-09-02_carry-only.md")
O13=$(stop 0   "2026-09-02_carry-only.md")

echo "$O11" | grep -q "auto-bound: _projects/$PROJ/tasks/1_in_progress/2026-09-02_carry-only.md" \
  && pass "positive marker: the hook ran and reported the deterministic exec-bind" \
  || fail "positive marker missing — the hook produced nothing on Stop 1: $O11"
[ "$(count_note "$TC1" '(auto) executed via [tasks:] carry; summary pending')" = "1" ] \
  && pass "the once-per-session exec-bind line is present exactly once" \
  || fail "exec-bind line count $(count_note "$TC1" '(auto) executed via [tasks:] carry; summary pending')"
echo "$O12" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: the carry alone opened a capture round (request block emitted)" \
  || fail "positive marker missing — no round opened from the carry: $O12"
[ "$(count_note "$TC1" '(auto) touched; summary pending (r')" = "0" ] \
  && pass "the carry-only round's expiry appended NO placeholder line" \
  || fail "carry-only expiry appended $(count_note "$TC1" '(auto) touched; summary pending (r') placeholder line(s): $(grep -F '(auto) touched; summary pending' "$TC1")"
unset O13

echo ""
echo "three further carry-only expiry cycles add no placeholder"
stop 0 "2026-09-02_carry-only.md" >/dev/null
stop 0 "2026-09-02_carry-only.md" >/dev/null
stop 0 "2026-09-02_carry-only.md" >/dev/null
[ "$(count_note "$TC1" '(auto) touched; summary pending (r')" = "0" ] \
  && pass "still zero placeholder lines after further carry-only expiries" \
  || fail "placeholder lines accumulated to $(count_note "$TC1" '(auto) touched; summary pending (r'): $(grep -F '(auto) touched; summary pending' "$TC1")"

echo ""
echo "POSITIVE CONTROL: the same harness with a real slice write DOES placeholder"
reset_state
TS2="$PDIR/tasks/1_in_progress/2026-09-02_slice-write.md"; mk "$TS2"
write_touched "$TS2"
O21=$(stop 999)
O22=$(stop 0)
echo "$O21" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: the slice write opened a capture round" \
  || fail "positive marker missing — no round opened from the slice: $O21"
[ "$(count_note "$TS2" '(auto) touched; summary pending (r1)')" = "1" ] \
  && pass "the slice-written task received its r1 placeholder (backstop intact)" \
  || fail "control arm did not fire — placeholder count $(count_note "$TS2" '(auto) touched; summary pending (r1)'); the harness is not exercising the backstop"
echo "$O22" | grep -q "auto-bound: _projects/$PROJ/tasks/1_in_progress/2026-09-02_slice-write.md" \
  && pass "the placeholder append was reported as an auto-bind" \
  || fail "no auto-bound report for the placeholder: $O22"

echo ""
echo "a carry-only round still applies a real summary when its sidecar lands"
reset_state
TC3="$PDIR/tasks/1_in_progress/2026-09-02_carry-summary.md"; mk "$TC3"
stop 999 "2026-09-02_carry-summary.md" >/dev/null
O31=$(stop 999 "2026-09-02_carry-summary.md")
echo "$O31" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: the carry-only round opened (spawn requested)" \
  || fail "positive marker missing — carry-only round never opened: $O31"
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-09-02_carry-summary.md","summary":"CARRYSUM the carry round's real judgement"}],
 "note_links":[],"proposals":[]}
EOF
O32=$(stop 999 "2026-09-02_carry-summary.md")
echo "$O32" | grep -q "applied summary: $PROJ/2026-09-02_carry-summary.md (r1)" \
  && pass "the carry-only round's real summary was applied and reported" \
  || fail "no applied summary for the carry-only round: $O32"
grep -qF "CARRYSUM the carry round's real judgement" "$TC3" \
  && pass "the summary text landed in the task @log" \
  || fail "summary text missing: $(grep -F "[s:$SID_TAG]" "$TC3")"

echo ""
echo "(referenced) over-bind must follow slice-note provenance, not the carry"
reset_state
NC="$PDIR/project-notes/procedures/carry-owned-note.md"; printf '# carry owned\n' > "$NC"
NCREL="project-notes/procedures/carry-owned-note.md"
TCO="$PDIR/tasks/1_in_progress/2026-09-02_carry-owner.md"; mk_linked "$TCO" "$NCREL"
NW="$PDIR/project-notes/procedures/slice-owned-note.md"; printf '# slice owned\n' > "$NW"
NWREL="project-notes/procedures/slice-owned-note.md"
TWO="$PDIR/tasks/1_in_progress/2026-09-02_slice-owner.md"; mk_linked "$TWO" "$NWREL"

write_touched "$NC"
stop 999 >/dev/null
stop 0   >/dev/null
grep -qF "(referenced) owner of $NCREL via reverse-index; capture expired (r1)" "$TCO" \
  && pass "setup: r1 legitimately over-bound the slice-note owner" \
  || fail "setup broken — these Stops must pass no [tasks:] carry, or the exec-bind pre-empts round 1's own (referenced) line; note owner carries: $(grep -F "[s:$SID_TAG]" "$TCO")"

write_touched "$NW"
O51=$(stop 999 "2026-09-02_carry-owner.md")
O52=$(stop 0   "2026-09-02_carry-owner.md")
echo "$O51" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: r2 opened with both the slice note and the carry" \
  || fail "positive marker missing — r2 never opened: $O51"
grep -qF "(referenced) owner of $NWREL via reverse-index; capture expired (r2)" "$TWO" \
  && pass "the SLICE-note owner still receives its (referenced) line at r2" \
  || fail "slice-note owner lost its r2 (referenced) line: $(grep -F "[s:$SID_TAG]" "$TWO")"
grep -qF "(referenced) owner of $NCREL via reverse-index; capture expired (r2)" "$TCO" \
  && fail "a carry-derived owner received a (referenced) line for a note round 2 never touched — this owner entered the round through the [tasks:] carry alone, never through a slice note: $(grep -F "[s:$SID_TAG]" "$TCO")" \
  || pass "no (referenced) line for the carry-derived owner at r2"
unset O52

echo ""
echo "=== Done ==="
