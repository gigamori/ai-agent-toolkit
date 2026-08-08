#!/usr/bin/env bash
# test_round_f1_integrity.sh — T-D1-6: the F-1 integrity rule
# (project-notes/specs/capture-detection-gaps.md §1.4, AC-7).
#
# D1 decides "did the agent log this round itself?" by comparing the task's
# `[s:sid8]` line COUNT against `log_seen`. The hook's own appends raise that
# same count, so without a correction they masquerade as an agent self-log and
# the NEXT round's real work is silently never summarized — a re-run of the
# exact silent-loss class D1 exists to remove. Two mechanisms guard it:
#
#   (a) the Stop hook resyncs `log_seen[T]` from every task IT appended to,
#       at the end of that Stop (§1.4 (a)). Pinned here for all three append
#       kinds: the G backstop placeholder, an applied capture summary, and the
#       deterministic `[tasks:]` exec-bind.
#   (b) `count_sid_lines` permanently EXCLUDES PreCompact placeholders (a note
#       starting with `_PRECOMPACT_NOTE_PREFIX`), because `precompact_flush.py`
#       cannot write `.bind` and so could never be resynced by (a) (§1.4 (b)).
#       W3 has not landed, so the placeholder line is written into the fixture
#       directly — exactly the bytes `precompact_flush.py` will append.
#
# Every section drives the REAL Stop hook; a failure of either mechanism shows
# up as "the round after the hook's own write never forms".
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted 250 real
# session-state files there). The real dir's file count is bracketed below.
#
# Usage:  bash plugins/taskflow/tests/test_round_f1_integrity.sh
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
PROJ="_test-f1-$$"
PDIR="$PROJECTS/$PROJ"
SID="f1integ$$-0000-0000-0000-000000000000"
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

mkdir -p "$PDIR/tasks/1_in_progress" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {  # $1 = task md path
  cat > "$1" << 'T'
---
priority: HIGH
---

# F-1 integrity test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->
T
}

write_touched() {  # $1 = absolute path under $PROJECTS → one ledger EVENT
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

stop() {  # $1 = expiry seconds; $2 = optional last_assistant_message
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_LAM="${2:-}" TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$HOOK")"
}

sidlines() {  # $1 = task md path → RAW [s:SID8] occurrences inside the @log block
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

# Append the line `precompact_flush.py` (W3) will write: the note begins with
# the module constant `_PRECOMPACT_NOTE_PREFIX`, read from the hook itself so
# this fixture cannot drift from the implementation.
precompact_line() {  # $1 = task md path
  uv run --no-project python - "$(to_win "$HOOK")" "$1" "$SID8" << 'PY'
import importlib.util, os, sys
hook, path, sid8 = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(hook))
spec = importlib.util.spec_from_file_location("cap", hook)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
note = "%s; summary pending (r2)" % mod._PRECOMPACT_NOTE_PREFIX
c = open(path, encoding="utf-8").read()
at = c.index("<!-- @log:end -->")
line = "- 2026-08-09T11:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

count_helper() {  # $1 = task md path → hook's own count_sid_lines()
  uv run --no-project python - "$(to_win "$HOOK")" "$1" "$SID8" << 'PY'
import importlib.util, os, sys
hook, path, sid8 = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(hook))
spec = importlib.util.spec_from_file_location("cap", hook)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
print(mod.count_sid_lines(path, sid8))
PY
}

echo "=== T-D1-6: F-1 integrity — the hook's own appends must not read as self-log ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
echo ""

# =====================================================================
# T-D1-6a: the G backstop placeholder is a HOOK append. New touches after it
# must still form the next round.
# =====================================================================
echo "[T-D1-6a] after a backstop placeholder, new touches still form a round"
reset_state
TA="$PDIR/tasks/1_in_progress/2026-08-09_f1-backstop.md"; mk "$TA"
write_touched "$TA"
stop 0 >/dev/null                    # round 1 requested
stop 0 >/dev/null                    # expiry -> backstop placeholder (hook append)
[ "$(sidlines "$TA")" = "1" ] \
  && pass "precondition: the backstop wrote the round-1 placeholder" \
  || fail "precondition failed: $(sidlines "$TA")"
# D2 (§3.3): `log_seen` is keyed by the QUALIFIED `<project>/<basename>`.
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-backstop.md')")" = "1" ] \
  && pass "F-1 (a): log_seen resynced from the hook's own placeholder" \
  || fail "log_seen not resynced: $(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-backstop.md')")"
write_touched "$TA"                  # genuinely new work in round 2
OA=$(stop 0)
echo "$OA" | grep -q '"decision": *"block"' \
  && pass "round 2 forms (the placeholder was NOT read as an agent self-log)" \
  || fail "round 2 swallowed as self-logged: $OA"
[ "$(bindq 'c.get("round")')" = "2" ] \
  && pass "round counter advanced to 2" || fail "round: $(bindq 'c.get("round")')"
stop 0 >/dev/null                    # round 2 backstop
[ "$(sidlines "$TA")" = "2" ] \
  && pass "round 2 produced its own line (2 total)" || fail "round 2 line: $(sidlines "$TA")"

# =====================================================================
# T-D1-6b: an APPLIED capture summary is a hook append too.
# =====================================================================
echo ""
echo "[T-D1-6b] after an applied capture summary, new touches still form a round"
reset_state
TB="$PDIR/tasks/1_in_progress/2026-08-09_f1-apply.md"; mk "$TB"
write_touched "$TB"
stop 999 >/dev/null                  # round 1 requested (no expiry)
cat > "$CF" << 'EOF'
{"confirmed":[{"task":"2026-08-09_f1-apply.md","summary":"F1APPLYSUMMARY"}],"note_links":[],"proposals":[]}
EOF
stop 999 >/dev/null                  # apply (hook append)
[ "$(sidlines "$TB")" = "1" ] \
  && pass "precondition: the capture summary was applied" || fail "apply failed: $(sidlines "$TB")"
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-apply.md')")" = "1" ] \
  && pass "F-1 (a): log_seen resynced from the applied summary" \
  || fail "log_seen not resynced: $(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-apply.md')")"
write_touched "$TB"                  # genuinely new work in round 2
OB=$(stop 999)
echo "$OB" | grep -q '"decision": *"block"' \
  && pass "round 2 forms (the applied summary was NOT read as a self-log)" \
  || fail "round 2 swallowed as self-logged: $OB"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "$PROJ/2026-08-09_f1-apply.md" ] \
  && pass "round 2's closed item set holds the task again (qualified)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"

# =====================================================================
# T-D1-6c: the deterministic `[tasks:]` exec-bind is a hook append.
# =====================================================================
echo ""
echo "[T-D1-6c] after an exec-bind, a later touch of the same task forms a round"
reset_state
TC="$PDIR/tasks/1_in_progress/2026-08-09_f1-exec.md"; mk "$TC"
stop 0 "[tasks: 2026-08-09_f1-exec.md] executed by reference" >/dev/null
[ "$(sidlines "$TC")" = "1" ] \
  && pass "precondition: the exec-bind wrote its line" || fail "exec-bind failed: $(sidlines "$TC")"
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-exec.md')")" = "1" ] \
  && pass "F-1 (a): log_seen resynced from the exec-bind line" \
  || fail "log_seen not resynced: $(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_f1-exec.md')")"
write_touched "$TC"                  # the task itself is edited afterwards
OC=$(stop 0)
echo "$OC" | grep -q '"decision": *"block"' \
  && pass "the follow-up round forms (exec-bind line NOT read as a self-log)" \
  || fail "follow-up round swallowed as self-logged: $OC"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "$PROJ/2026-08-09_f1-exec.md" ] \
  && pass "the task is back in the round's closed item set (qualified)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"

# =====================================================================
# T-D1-6d: F-1 (b) — a PreCompact placeholder is NOT counted, so it cannot
# block the next round. `precompact_flush.py` never writes `.bind`, so this
# line can never be resynced by (a); excluding it from the count is the only
# thing keeping the post-compaction round alive.
# =====================================================================
echo ""
echo "[T-D1-6d] a PreCompact placeholder does not block the next round's formation"
reset_state
TD="$PDIR/tasks/1_in_progress/2026-08-09_f1-precompact.md"; mk "$TD"
write_touched "$TD"
stop 0 >/dev/null                    # round 1 requested
stop 0 >/dev/null                    # expiry -> round-1 placeholder
[ "$(sidlines "$TD")" = "1" ] && pass "precondition: round 1 bound" || fail "round 1: $(sidlines "$TD")"
precompact_line "$TD"                # what precompact_flush.py appends at compaction
[ "$(sidlines "$TD")" = "2" ] \
  && pass "precondition: the PreCompact line is physically present (2 raw [s:] lines)" \
  || fail "PreCompact fixture line missing: $(sidlines "$TD")"
[ "$(count_helper "$TD")" = "1" ] \
  && pass "F-1 (b): count_sid_lines excludes the PreCompact placeholder (1, not 2)" \
  || fail "count_sid_lines counted the PreCompact line: $(count_helper "$TD")"
write_touched "$TD"                  # post-compaction work
OD=$(stop 0)
echo "$OD" | grep -q '"decision": *"block"' \
  && pass "the post-compaction round still forms" \
  || fail "PreCompact placeholder blocked the round: $OD"
[ "$(bindq 'c.get("round")')" = "2" ] \
  && pass "round counter advanced to 2 across the compaction" \
  || fail "round: $(bindq 'c.get("round")')"
stop 0 >/dev/null                    # round 2 backstop
grep -qF "[s:$SID8]: (auto) touched; summary pending (r2)" "$TD" \
  && pass "round 2 landed its own placeholder next to the PreCompact line" \
  || fail "round 2 placeholder missing: $(grep -F "[s:$SID8]" "$TD")"

echo ""
echo "=== Done ==="
