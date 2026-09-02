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
PROJ="_test-nsdebt"
PDIR="$PROJECTS/$PROJ"
SID="d0eb7000-8888-4888-8888-888888888888"
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
  [ "$FAIL" -eq 0 ] || exit 1
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

# Next Steps freshness test task

## Goal
- pin the ns freshness mechanism

## Next Steps
- ORIGINAL step one
- ORIGINAL step two

<!-- @log:begin -->
- 2026-09-02: created
<!-- @log:end -->

<!-- @notes:begin -->
<!-- auto-managed by taskflow note-link; do not hand-edit -->
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
    uv run --no-project python -c "import json,os,sys;sys.stdout.write(json.dumps({'session_id':os.environ['TASKFLOW_SID'],'last_assistant_message':'no carry.'}))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

bind_get() {
  uv run --no-project python - "$BF" "$1" << 'PY'
import json, sys
try:
    cur = json.load(open(sys.argv[1], encoding="utf-8")).get("capture", {})
except (OSError, ValueError):
    cur = {}
for part in sys.argv[2].split(".", 1):
    if isinstance(cur, dict) and part in cur:
        cur = cur[part]
    else:
        cur = ""
        break
print(cur if not isinstance(cur, (dict, list)) else json.dumps(cur, sort_keys=True))
PY
}

ns_md5() {
  uv run --no-project python - "$1" << 'PY'
import hashlib, re, sys
raw = open(sys.argv[1], "rb").read().replace(b"\r\n", b"\n")
m = re.search(rb"^##\s+Next Steps\s*$(.*?)(?=^##\s|<!--\s*@log:begin|\Z)",
              raw, re.DOTALL | re.MULTILINE)
print(hashlib.md5(m.group(1) if m else b"").hexdigest())
PY
}

ns_write() {
  uv run --no-project python - "$1" "$2" << 'PY'
import re, sys
path, mode = sys.argv[1], sys.argv[2]
raw = open(path, "rb").read()
m = re.search(rb"^(##\s+Next Steps\s*\n)(.*?)(?=^##\s|<!--\s*@log:begin|\Z)",
              raw, re.DOTALL | re.MULTILINE)
body = m.group(2)
new = body.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n") if mode == "crlf" \
    else b"- REWRITTEN step one\n- REWRITTEN step two\n\n"
open(path, "wb").write(raw[:m.start(2)] + new + raw[m.end(2):])
PY
}

KEY_PREFIX="$PROJ/"

echo "=== Next Steps freshness debt ==="
echo "  project=$PROJ  tag=$SID_TAG  (isolated tempdir: $TMP)"
echo ""

echo "a real summary applied while ## Next Steps stayed unchanged"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-09-02_ns-stale.md"; mk "$T1"
K1="${KEY_PREFIX}2026-09-02_ns-stale.md"
NS_BEFORE="$(ns_md5 "$T1")"
write_touched "$T1"
O11=$(stop 999)
echo "$O11" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: round 1 opened (request block emitted)" \
  || fail "positive marker missing — round 1 never opened: $O11"
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-09-02_ns-stale.md","summary":"NSSUM1 round one's real judgement"}],
 "note_links":[],"proposals":[]}
EOF
O12=$(stop 999)
echo "$O12" | grep -q "applied summary: $K1 (r1)" \
  && pass "positive marker: round 1's REAL summary was applied (capture path is live)" \
  || fail "positive marker missing — no applied summary, the arm below is vacuous: $O12"
[ "$(bind_get "ns_debt.$K1")" = "1" ] \
  && pass "ns_debt is 1 after a real summary with an unchanged ## Next Steps" \
  || fail "capture.ns_debt['$K1'] = '$(bind_get "ns_debt.$K1")' (expected 1); ns_debt=$(bind_get ns_debt)"

write_touched "$T1"
O13=$(stop 999)
echo "$O13" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: round 2 opened, so a context block was emitted" \
  || fail "positive marker missing — round 2 never opened: $O13"
echo "$O13" | grep -q "next_steps_stale" \
  && pass "the next capture request's context block carries next_steps_stale" \
  || fail "no next_steps_stale in the round-2 context block: $(echo "$O13" | grep -o '{\"sid\".*}' | head -c 600)"

cat > "$(rcap 2)" << EOF
{"confirmed":[{"task":"2026-09-02_ns-stale.md","summary":"NSSUM2 round two's real judgement"}],
 "note_links":[],"proposals":[],
 "next_steps":[{"task":"2026-09-02_ns-stale.md","proposal":"NSPROP rewrite the Next Steps to name the landed phase"}]}
EOF
O14=$(stop 999)
echo "$O14" | grep -q "applied summary: $K1 (r2)" \
  && pass "positive marker: round 2's real summary was applied" \
  || fail "positive marker missing — round 2's summary never applied: $O14"
NPROP=$(echo "$O14" | grep -cF "NSPROP rewrite the Next Steps")
[ "$NPROP" = "1" ] \
  && pass "the rewrite proposal was surfaced in the Stop block exactly once" \
  || fail "rewrite proposal occurrences = $NPROP (expected 1): $O14"
[ "$(ns_md5 "$T1")" = "$NS_BEFORE" ] \
  && pass "the hook did NOT rewrite the task's ## Next Steps body (LF-normalized)" \
  || fail "the ## Next Steps body changed on disk after LF normalization ($NS_BEFORE -> $(ns_md5 "$T1")); this digest must stay LF-normalized, never raw bytes"

write_touched "$T1"
O15=$(stop 999)
O16=$(stop 999)
REPEAT=$(printf '%s\n%s\n' "$O15" "$O16" | grep -cF "NSPROP rewrite the Next Steps")
[ "$REPEAT" = "0" ] \
  && pass "the proposal is not re-presented on later Stops (打止め)" \
  || fail "the proposal was re-presented $REPEAT time(s)"
echo "$O15" | grep -q "next_steps_stale.*$K1" \
  && fail "the key is still listed in next_steps_stale after being presented once" \
  || pass "a presented key is no longer listed in next_steps_stale"

echo ""
echo "NEGATIVE ARM: the same flow with ## Next Steps actually rewritten"
reset_state
T2="$PDIR/tasks/1_in_progress/2026-09-02_ns-fresh.md"; mk "$T2"
K2="${KEY_PREFIX}2026-09-02_ns-fresh.md"
write_touched "$T2"
O21=$(stop 999)
echo "$O21" | grep -q 'Spawn the async capture subagent' \
  && pass "positive marker: round 1 opened for the negative arm" \
  || fail "positive marker missing — negative arm's round 1 never opened: $O21"
ns_write "$T2" rewrite
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-09-02_ns-fresh.md","summary":"NSFRESH1 round one's real judgement"}],
 "note_links":[],"proposals":[]}
EOF
O22=$(stop 999)
echo "$O22" | grep -q "applied summary: $K2 (r1)" \
  && pass "positive marker: the negative arm applied a real summary too" \
  || fail "positive marker missing — negative arm applied nothing: $O22"
[ "$(bind_get "ns_debt.$K2")" = "0" ] \
  && pass "a genuinely rewritten ## Next Steps leaves ns_debt at 0" \
  || fail "capture.ns_debt['$K2'] = '$(bind_get "ns_debt.$K2")' (expected 0); ns_debt=$(bind_get ns_debt)"
write_touched "$T2"
O23=$(stop 999)
echo "$O23" | grep -q "next_steps_stale.*$K2" \
  && fail "next_steps_stale named a task whose ## Next Steps was rewritten: $O23" \
  || pass "no next_steps_stale for the rewritten task"

echo ""
echo "NORMALIZATION PIN: a CRLF-only change must not clear the debt"
reset_state
T3="$PDIR/tasks/1_in_progress/2026-09-02_ns-crlf.md"; mk "$T3"
K3="${KEY_PREFIX}2026-09-02_ns-crlf.md"
write_touched "$T3"
stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[{"task":"2026-09-02_ns-crlf.md","summary":"NSCRLF1 round one's real judgement"}],
 "note_links":[],"proposals":[]}
EOF
O31=$(stop 999)
echo "$O31" | grep -q "applied summary: $K3 (r1)" \
  && pass "positive marker: the CRLF arm applied round 1's real summary" \
  || fail "positive marker missing — CRLF arm applied nothing: $O31"
ns_write "$T3" crlf
write_touched "$T3"
stop 999 >/dev/null
cat > "$(rcap 2)" << EOF
{"confirmed":[{"task":"2026-09-02_ns-crlf.md","summary":"NSCRLF2 round two's real judgement"}],
 "note_links":[],"proposals":[]}
EOF
O32=$(stop 999)
echo "$O32" | grep -q "applied summary: $K3 (r2)" \
  && pass "positive marker: the CRLF arm applied round 2's real summary" \
  || fail "positive marker missing — CRLF arm's round 2 applied nothing: $O32"
[ "$(bind_get "ns_debt.$K3")" = "2" ] \
  && pass "a CRLF-only change is normalized away — the debt kept counting (2)" \
  || fail "capture.ns_debt['$K3'] = '$(bind_get "ns_debt.$K3")' (expected 2); a raw-byte digest would read 0 here; ns_debt=$(bind_get ns_debt)"

echo ""
echo "T3: next_steps proposals render under their own label, genesis proposals unchanged"
reset_state
TQ="$PDIR/tasks/1_in_progress/2026-09-02_ns-quiet.md"; mk "$TQ"
NQ="$PDIR/project-notes/procedures/ns-quiet-note.md"; printf '# quiet note\n' > "$NQ"
write_touched "$NQ"
OQ1=$(stop 999)
echo "$OQ1" | grep -q 'Spawn the async capture subagent' \
  && pass "T3 quiet fixture: round 1 opened via a note touch (task set stays empty)" \
  || fail "T3 quiet fixture: round 1 never opened: $OQ1"
cat > "$(rcap 1)" << EOF
{"confirmed":[],"note_links":[],"proposals":[],
 "next_steps":[{"task":"2026-09-02_ns-quiet.md","proposal":"QUIETPROP rewrite this task Next Steps"}]}
EOF

echo ""
echo "arm 1: a Stop whose only content is a next_steps proposal still emits a block"
OQ2=$(stop 999)
echo "$OQ2" | grep -qF 'Proposed Next Steps rewrites' \
  && pass "the next-steps heading is present" \
  || fail "next-steps heading missing: $OQ2"
echo "$OQ2" | grep -qF '(next-steps) QUIETPROP rewrite this task Next Steps' \
  && pass "the proposal renders with the (next-steps) prefix" \
  || fail "(next-steps) entry missing: $OQ2"
echo "$OQ2" | grep -qF 'Proposed new tasks' \
  && fail "the genesis 'Proposed new tasks' heading appeared for a next_steps-only round: $OQ2" \
  || pass "no genesis heading for a next_steps-only round"
echo "$OQ2" | grep -qF 'Spawn the async capture subagent' \
  && fail "a new capture was unexpectedly re-requested this Stop (spawn=true) — the quiet-fixture precondition is violated" \
  || pass "no new capture requested this Stop (spawn=false precondition holds)"
[ -n "$OQ2" ] \
  && pass "a block WAS emitted for a next_steps-only round (catches a missed gate edit)" \
  || fail "NO block was emitted for a next_steps-only round — the gate still excludes ns_proposals"

echo ""
echo "arm 2: a sidecar with genesis proposals only renders under the genesis heading, no next-steps heading"
reset_state
TG="$PDIR/tasks/1_in_progress/2026-09-02_ns-genesis.md"; mk "$TG"
NG="$PDIR/project-notes/procedures/ns-genesis-note.md"; printf '# genesis note\n' > "$NG"
write_touched "$NG"
stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[],"note_links":[],"proposals":["GENESISPROP a brand new task idea"]}
EOF
OG2=$(stop 999)
echo "$OG2" | grep -qF 'Proposed new tasks' \
  && pass "the genesis heading is present" \
  || fail "genesis heading missing: $OG2"
echo "$OG2" | grep -qF '(proposed) GENESISPROP a brand new task idea' \
  && pass "the genesis proposal renders with the (proposed) prefix, unchanged" \
  || fail "(proposed) entry missing: $OG2"
echo "$OG2" | grep -qF 'Proposed Next Steps rewrites' \
  && fail "the next-steps heading appeared for a genesis-only round: $OG2" \
  || pass "no next-steps heading for a genesis-only round"

echo ""
echo "arm 3: a sidecar with both kinds renders both headings, each item under its own"
reset_state
TB="$PDIR/tasks/1_in_progress/2026-09-02_ns-both.md"; mk "$TB"
NB="$PDIR/project-notes/procedures/ns-both-note.md"; printf '# both note\n' > "$NB"
write_touched "$NB"
stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[],"note_links":[],"proposals":["BOTHGENESIS a genesis idea"],
 "next_steps":[{"task":"2026-09-02_ns-both.md","proposal":"BOTHNS rewrite this task Next Steps"}]}
EOF
OB2=$(stop 999)
echo "$OB2" | grep -qF 'Proposed new tasks' \
  && pass "both: the genesis heading is present" \
  || fail "both: genesis heading missing: $OB2"
echo "$OB2" | grep -qF 'Proposed Next Steps rewrites' \
  && pass "both: the next-steps heading is present" \
  || fail "both: next-steps heading missing: $OB2"
echo "$OB2" | grep -qF '(proposed) BOTHGENESIS a genesis idea' \
  && pass "both: the genesis item carries the (proposed) prefix" \
  || fail "both: genesis item missing: $OB2"
echo "$OB2" | grep -qF '(next-steps) BOTHNS rewrite this task Next Steps' \
  && pass "both: the next-steps item carries the (next-steps) prefix" \
  || fail "both: next-steps item missing: $OB2"

echo ""
echo "arm 4: the same quiet fixture with next_steps removed emits no block at all"
reset_state
TQ4="$PDIR/tasks/1_in_progress/2026-09-02_ns-quiet2.md"; mk "$TQ4"
NQ4="$PDIR/project-notes/procedures/ns-quiet2-note.md"; printf '# quiet2 note\n' > "$NQ4"
write_touched "$NQ4"
stop 999 >/dev/null
cat > "$(rcap 1)" << EOF
{"confirmed":[],"note_links":[],"proposals":[]}
EOF
OQ4=$(stop 999)
[ -z "$OQ4" ] \
  && pass "the quiet fixture with no next_steps field emits NO block at all (proves arm 1's block came from ns_proposals)" \
  || fail "the quiet-minus-next_steps fixture unexpectedly emitted a block: $OQ4"

echo ""
echo "=== Done ==="
