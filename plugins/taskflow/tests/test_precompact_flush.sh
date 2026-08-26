#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PC="$REPO_ROOT/plugins/taskflow/hooks/precompact_flush.py"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJ="_test-precompact-$$"
PDIR="$PROJECTS/$PROJ"
SID="precomp$$-0000-0000-0000-000000000000"
SID_CLEAN="${SID//-/}"; SID_TAG="${SID_CLEAN: -12}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"; CF="$STATE/$SID.capture"
OUTF="$TMP/pc.out"

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

mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/specs" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"
printf '# progress\n' > "$PDIR/progress.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {
  cat > "$1" << 'T'
---
priority: HIGH
---

# PreCompact flush test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->
T
}

write_touched() {
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

bind_fp() {
  if [ -f "$BF" ]; then md5sum < "$BF" | cut -d' ' -f1; else echo "ABSENT"; fi
}

precompact() {
  TASKFLOW_SID="$SID" TASKFLOW_TRIG="${1:-manual}" \
    uv run --no-project python -c "import json,os,sys;sys.stdout.write(json.dumps({'session_id':os.environ['TASKFLOW_SID'],'transcript_path':'x.jsonl','cwd':os.getcwd(),'prompt_id':'p1','hook_event_name':'PreCompact','trigger':os.environ['TASKFLOW_TRIG'],'custom_instructions':None}))" \
    | uv run --no-project python "$(to_win "$PC")" > "$OUTF"
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
line = "- 2026-08-09T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

PLACE="(auto) unflushed at compaction; summary pending"
STDOUT_PREFIX="Preserve verbatim in the summary: unwritten per-task progress (results, decisions, remaining steps) for:"

echo "=== PreCompact flush ==="
echo "  project=$PROJ  tag=$SID_TAG  (isolated tempdir: $TMP)"
  echo ""

echo "[A] pending non-empty -> placeholder (r1) + exactly one stdout line"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-08-09_precompact.md"; mk "$T1"
write_touched "$T1"
FP0=$(bind_fp)
precompact manual
[ "$(sidlines "$T1")" = "1" ] \
  && pass "the flush appended exactly one [s:$SID_TAG] line" \
  || fail "line count: $(sidlines "$T1")"
grep -qF "[s:$SID_TAG]: $PLACE (r1)" "$T1" \
  && pass "the placeholder note is '$PLACE (r1)' (N = capture.round+1 = 1)" \
  || fail "note wrong: $(grep -F "[s:$SID_TAG]" "$T1")"
[ "$(wc -l < "$OUTF")" = "1" ] \
  && pass "stdout is exactly one line" || fail "stdout lines: $(wc -l < "$OUTF")"
grep -qF "$STDOUT_PREFIX $PROJ/2026-08-09_precompact.md" "$OUTF" \
  && pass "stdout carries the verbatim-preservation instruction + the qualified task" \
  || fail "stdout wrong: $(cat "$OUTF")"
grep -q '^[[:space:]]*[{[]' "$OUTF" \
 && fail "stdout looks like JSON —: JSON is pasted verbatim, never parsed" \
 || pass "stdout is plain text, not JSON"
[ "$(bind_fp)" = "$FP0" ] && [ "$FP0" = "ABSENT" ] \
 && pass ".bind was NOT created by the flush (read-only, step 4)" \
  || fail ".bind changed: $FP0 -> $(bind_fp)"

  echo ""
echo "[B] flushing twice in one round adds only one line (text key,)"
precompact auto
[ "$(sidlines "$T1")" = "1" ] \
  && pass "second flush added NO second line" || fail "line count: $(sidlines "$T1")"
[ "$(grep -cF "$PLACE (r1)" "$T1")" = "1" ] \
  && pass "the (r1) placeholder text appears exactly once" \
  || fail "placeholder occurrences: $(grep -cF "$PLACE (r1)" "$T1")"
[ "$(wc -l < "$OUTF")" = "1" ] \
  && pass "the second flush still emits its one-line instruction (still pending)" \
  || fail "stdout lines: $(wc -l < "$OUTF")"
[ "$(bind_fp)" = "ABSENT" ] \
  && pass ".bind still absent after the second flush" || fail ".bind appeared: $(bind_fp)"

  echo ""
echo "[E] the Stop after a compaction still forms its round"
OE1=$(stop 0)
echo "$OE1" | grep -q '"decision": *"block"' \
  && pass "the post-compaction Stop still requests a capture" \
  || fail "round did not form after the flush: $OE1"
[ "$(bindq 'c.get("round")')" = "1" ] \
  && pass ".bind capture.round advanced to 1" || fail "round: $(bindq 'c.get("round")')"
stop 0 >/dev/null
grep -qF "[s:$SID_TAG]: (auto) touched; summary pending (r1)" "$T1" \
  && pass "the round's own backstop line coexists with the compaction placeholder" \
  || fail "backstop missing: $(grep -F "[s:$SID_TAG]" "$T1")"
[ "$(sidlines "$T1")" = "2" ] \
  && pass "task now carries 2 lines (compaction placeholder + r1 backstop)" \
  || fail "line count: $(sidlines "$T1")"

  echo ""
echo "[D] pending empty -> zero stdout bytes, .bind byte-identical"
FPD=$(bind_fp)
[ "$FPD" != "ABSENT" ] || fail "precondition: .bind should exist by now"
precompact manual
[ "$(wc -c < "$OUTF")" = "0" ] \
  && pass "stdout is ZERO bytes (precomputed-compaction reuse preserved)" \
  || fail "stdout bytes: $(wc -c < "$OUTF") — $(cat "$OUTF")"
[ "$(sidlines "$T1")" = "2" ] \
  && pass "no line was added for an empty pending set" || fail "line count: $(sidlines "$T1")"
[ "$(bind_fp)" = "$FPD" ] \
  && pass ".bind is byte-identical across the invocation ($FPD)" \
  || fail ".bind CHANGED: $FPD -> $(bind_fp)"

  echo ""
echo "[F] a flush after round 1 tags the placeholder (r2)"
FPF=$(bind_fp)
write_touched "$T1"
precompact manual
grep -qF "[s:$SID_TAG]: $PLACE (r2)" "$T1" \
  && pass "the new placeholder is tagged (r2) = capture.round+1" \
  || fail "note wrong: $(grep -F "$PLACE" "$T1")"
[ "$(sidlines "$T1")" = "3" ] \
  && pass "the (r2) key is distinct from (r1) — a third line landed" \
  || fail "line count: $(sidlines "$T1")"
[ "$(bind_fp)" = "$FPF" ] \
  && pass ".bind still byte-identical after the second-round flush" \
  || fail ".bind CHANGED: $FPF -> $(bind_fp)"

  echo ""
echo "[D2] a self-logged round is silent (no placeholder, no stdout)"
reset_state
T2="$PDIR/tasks/1_in_progress/2026-08-09_precompact-selflog.md"; mk "$T2"
write_touched "$T2"
agent_log_line "$T2" "agent wrote its own progress line"
precompact manual
[ "$(wc -c < "$OUTF")" = "0" ] \
  && pass "self-logged round produces ZERO stdout bytes" \
  || fail "stdout: $(cat "$OUTF")"
[ "$(sidlines "$T2")" = "1" ] \
  && pass "no placeholder on top of the agent's own line" \
  || fail "line count: $(sidlines "$T2")"

  echo ""
echo "[G] out-of-scope sessions are silent no-ops"
mv "$SF" "$SF.hidden"
precompact manual
[ "$(wc -c < "$OUTF")" = "0" ] \
  && pass "no session-state json -> zero stdout bytes" || fail "stdout: $(cat "$OUTF")"
printf '{"session_id":"%s","project":""}\n' "$SID" > "$SF"
precompact manual
[ "$(wc -c < "$OUTF")" = "0" ] \
  && pass "empty project -> zero stdout bytes" || fail "stdout: $(cat "$OUTF")"
mv "$SF.hidden" "$SF"

  echo ""
echo "=== Done ==="
