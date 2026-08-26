#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

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
PROJ="_test-selflog-$$"
PDIR="$PROJECTS/$PROJ"
SID="selflog$$-0000-0000-0000-000000000000"
SID_CLEAN="${SID//-/}"; SID_TAG="${SID_CLEAN: -12}"
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
  [ "$FAIL" -eq 0 ] || exit 1
}
trap cleanup EXIT

mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/specs" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {
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
  uv run --no-project python - "$1" "$SID_TAG" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

bindq() {
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

agent_log_line() {
  uv run --no-project python - "$1" "$SID_TAG" "$2" << 'PY'
import sys
path, sid8, note = sys.argv[1], sys.argv[2], sys.argv[3]
c = open(path, encoding="utf-8").read()
at = c.index("<!-- @log:end -->")
line = "- 2026-08-19T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

echo "=== self-logged task gets NO expiry placeholder (04-plan) ==="
echo "  project=$PROJ  tag=$SID_TAG  (isolated tempdir: $TMP)"

  echo ""
echo "[defect arm] self-logged task: in allow_tasks, out of items.tasks, no placeholder"
reset_state
TD="$PDIR/tasks/1_in_progress/2026-08-19_selflog-defect.md"; mk "$TD"
ND="$PDIR/project-notes/specs/selflog-defect-note.md"; printf '# defect note\n' > "$ND"
write_touched "$TD"
write_touched "$ND"
agent_log_line "$TD" "agent wrote its own progress line"
OD1=$(stop 0)

echo "$OD1" | grep -q '"decision": *"block"' \
  && pass "the round opened (capture requested) — the arm reaches the backstop path" \
  || fail "no capture was requested, so expiry never runs: $OD1"
[ "$(bindq 'c.get("round")')" = "1" ] \
  && pass "round 1 is open" || fail "round: $(bindq 'c.get("round")')"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "" ] \
  && pass "items.tasks is EMPTY — the self-logged task was subtracted" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"
[ "$(bindq '",".join(((c.get("items") or {}).get("allow_tasks") or []))')" = "$PROJ/2026-08-19_selflog-defect.md" ] \
  && pass "items.allow_tasks DOES carry it — the membership gate stayed open" \
  || fail "items.allow_tasks: $(bindq '",".join(((c.get("items") or {}).get("allow_tasks") or []))')"
[ "$(bindq '",".join(((c.get("items") or {}).get("notes") or []))')" = "project-notes/specs/selflog-defect-note.md" ] \
  && pass "the novel note is what opened the round" \
  || fail "items.notes: $(bindq '",".join(((c.get("items") or {}).get("notes") or []))')"

OD2=$(stop 0)

[ "$(sidlines "$TD")" = "1" ] \
  && pass "still exactly ONE [s:sid8] line — the agent's own, nothing stapled to it" \
  || fail "line count changed at expiry: $(sidlines "$TD")"
if grep -qF "$PLACEHOLDER" "$TD"; then
  fail "PLACEHOLDER LEAKED onto a self-logged task: $(grep -F "$PLACEHOLDER" "$TD")"
  else
 pass "no '$PLACEHOLDER' line on the self-logged task"
  fi
if echo "$OD2" | grep -qF "auto-bound"; then
  fail "expiry reported an auto-bind for a self-logged round: $OD2"
  else
  pass "expiry reported no auto-bind at all"
  fi

  echo ""
echo "[control arm] same round without the self-log: the placeholder DOES fire"
reset_state
TC="$PDIR/tasks/1_in_progress/2026-08-19_selflog-control.md"; mk "$TC"
NC="$PDIR/project-notes/specs/selflog-control-note.md"; printf '# control note\n' > "$NC"
write_touched "$TC"
write_touched "$NC"
OC1=$(stop 0)

echo "$OC1" | grep -q '"decision": *"block"' \
  && pass "the control round opened too (same fixture, same path)" \
  || fail "control round did not open: $OC1"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "$PROJ/2026-08-19_selflog-control.md" ] \
  && pass "items.tasks carries the un-logged task (nothing to subtract)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"
[ "$(sidlines "$TC")" = "0" ] \
  && pass "no line yet at request time" || fail "premature line: $(sidlines "$TC")"

OC2=$(stop 0)

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
