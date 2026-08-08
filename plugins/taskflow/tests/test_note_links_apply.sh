#!/usr/bin/env bash
# test_note_links_apply.sh — end-to-end test of the §10 async capture apply-path
# in session_progress_capture.py (note-task-link.md Phase B).
#
# The capture subagent's agent def is NOT hot-reloaded and a live `claude` is not
# available here, so this test SIMULATES the subagent by writing the `<sid>.capture`
# sidecar directly (the subagent's only artifact, §10.5) and drives the REAL Stop
# hook to apply it. It exercises the deterministic apply / lifecycle / expiry:
#
#   AC-9   in-flight: a requested capture does NOT re-spawn while pending (<expiry)
#   AC-1   write→establish: note_links sidecar entry links the note in task @notes
#   AC-8   writer separation: writing the sidecar does NOT touch @log/@notes; only
#          the Stop hook applies them
#   AC-11  apply is idempotent + eventual: the real summary wins over a placeholder
#          and re-running the hook produces no duplicates
#   AC-10  expiry (15s, forced to 0): G placeholder binds touched tasks; a note
#          write whose owner is known via the reverse index gets a `referenced`
#          over-bind; an unlinked note is NOT established under judgment-absent expiry
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md, project-notes/specs/
# capture-hook-sweep-sandbox.md): the Stop hook runs an unconditional stale-marker
# sweep on every invocation and resolves `_projects` via getcwd() (no env override).
# This test therefore `cd`s into an isolated tempdir and builds `_projects/` there —
# it NEVER cd's into $REPO_ROOT while invoking the hook, so the sweep can never
# reach the real _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted
# 250 real session-state files there).
#
# Usage:  bash plugins/taskflow/tests/test_note_links_apply.sh
# Requires: bash (Git-Bash on win32 — primary), uv.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
STOP="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
NL="$REPO_ROOT/plugins/taskflow/hooks/note_links.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"

TMP="$(mktemp -d)"
cd "$TMP"
PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJ="_e2e-apply-$$"
PDIR="$PROJECTS/$PROJ"
SID="e2eaply$$-0000-0000-0000-000000000000"
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
  cd "$REPO_ROOT"
  rm -rf "$TMP"
  echo ""
  if [ "$FAIL" -eq 0 ]; then echo "All $PASS tests passed."; else echo "$FAIL failed, $PASS passed."; fi
}
trap cleanup EXIT
mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/specs" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"

reset_state() { rm -f "$TF" "$BF" "$CF"; }

mk() {  # $1 = task md path (with @log, no @notes)
  cat > "$1" << 'T'
---
priority: HIGH
---

# E2E apply task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-07-01: created
<!-- @log:end -->
T
}

mk_linked() {  # $1 = task md path, $2 = note project-rel to pre-link in @notes
  cat > "$1" << T
---
priority: HIGH
---

# E2E linked-owner task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-07-01: created
<!-- @log:end -->

<!-- @notes:begin -->
<!-- auto-managed by taskflow note-link; do not hand-edit -->
- $2
<!-- @notes:end -->
T
}

write_touched() {  # $1 = absolute path under $PROJECTS → append repo-relative-style line
  local rel="${1#$PROJECTS/}"
  rel="_projects/${rel}"
  rel="${rel//\\//}"
  printf '%s\n' "$rel" >> "$TF"
}

# Simulate the capture subagent's sole artifact: write <sid>.capture JSON.
write_capture() {  # $1=task base (summary) $2=summary $3=note projrel $4=note-link task base
  uv run --no-project python - "$CF" "${1:-}" "${2:-}" "${3:-}" "${4:-}" << 'PY'
import json, sys
cf, task, summary, note, ntask = (sys.argv + ["", "", "", "", ""])[1:6]
obj = {"confirmed": [], "note_links": [], "proposals": []}
if task:
    obj["confirmed"].append({"task": task, "summary": summary})
if note and ntask:
    obj["note_links"].append({"note": note, "task": ntask})
open(cf, "w", encoding="utf-8").write(json.dumps(obj))
PY
}

stop() {  # $1 = expiry seconds; $2 = optional last_assistant_message
  # `export` so the Stop hook (the SECOND pipeline stage) inherits the expiry —
  # an inline `VAR=x cmd1 | cmd2` prefix binds only cmd1, not the hook.
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_LAM="${2:-}" TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
    | uv run --no-project python "$(to_win "$STOP")"
}

sidlines() {  # $1 = task md path → count [s:SID8] inside @log block
  uv run --no-project python - "$1" "$SID8" << 'PY'
import re, sys
c = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"<!--\s*@log:begin\s*-->(.*?)<!--\s*@log:end\s*-->", c, re.DOTALL)
print((m.group(1) if m else "").count("[s:%s]" % sys.argv[2]))
PY
}

notecount() {  # $1 = task md path, $2 = note project-rel → count in @notes block
  uv run --no-project python - "$1" "$2" "$NL" << 'PY'
import sys, os
task, note, nlpath = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(nlpath))
import note_links as nl
print(nl.parse_note_links(open(task, encoding="utf-8").read()).count(note))
PY
}

echo "=== E2E: §10 async capture apply-path (note-task-link Phase B) ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
echo ""

# =====================================================================
# AC-9: in-flight requested capture does NOT re-spawn while pending (<expiry).
# =====================================================================
echo "[AC-9] in-flight: requested capture does not double-spawn (pending, no bind)"
reset_state
T9="$PDIR/tasks/1_in_progress/2026-07-01_ac9.md"; mk "$T9"
write_touched "$T9"
O9A=$(stop 999)
echo "$O9A" | grep -q '"decision": *"block"' \
  && pass "Stop#1 requests capture (decision:block spawn)" || fail "Stop#1 no spawn-block: $O9A"
[ "$(sidlines "$T9")" = "0" ] && pass "Stop#1 does not bind (requested, not placeholder)" || fail "premature bind at request"
O9B=$(stop 999)
if [ -z "$O9B" ] || ! echo "$O9B" | grep -q '"decision": *"block"'; then
  pass "Stop#2 (sidecar absent, <expiry) does NOT block → pending, no re-spawn (AC-9)"
else
  fail "Stop#2 re-spawned while in-flight: $O9B"
fi
[ "$(sidlines "$T9")" = "0" ] && pass "pending state still does not bind" || fail "bound while pending"

# =====================================================================
# AC-1 / AC-8 / AC-11: sidecar apply establishes the note link + real summary.
# =====================================================================
echo ""
echo "[AC-1/AC-8/AC-11] sidecar apply: note link established, real summary wins, idempotent"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-07-01_ac1.md"; mk "$T1"
T1BASE="$(basename "$T1")"
N1ABS="$PDIR/project-notes/specs/ac1-note.md"; printf '# ac1 note\n' > "$N1ABS"
N1REL="project-notes/specs/ac1-note.md"
write_touched "$T1"; write_touched "$N1ABS"

O1A=$(stop 999)
echo "$O1A" | grep -q '"decision": *"block"' \
  && pass "Stop#1 requests capture (task missing + unlinked note)" || fail "Stop#1 no request: $O1A"

# AC-8: the capture subagent writes ONLY the sidecar; the task md is untouched
# until the Stop hook applies it.
BEFORE_HASH=$(uv run --no-project python - "$T1" << 'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())
PY
)
write_capture "$T1BASE" "REALSUMMARYAC1" "$N1REL" "$T1BASE"
AFTER_HASH=$(uv run --no-project python - "$T1" << 'PY'
import hashlib, sys
print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())
PY
)
[ "$BEFORE_HASH" = "$AFTER_HASH" ] \
  && pass "AC-8: writing the sidecar did NOT modify the task @log/@notes" \
  || fail "AC-8: sidecar write changed the task file"

O1B=$(stop 999)
[ "$(sidlines "$T1")" = "1" ] && pass "AC-11: Stop applies one [s:$SID8] @log line" || fail "apply did not bind: $(sidlines "$T1")"
grep -q "\[s:$SID8\]: REALSUMMARYAC1" "$T1" \
  && pass "AC-11: real capture summary wins (not a placeholder)" || fail "real summary not applied"
! grep -q "(auto) touched; summary pending" "$T1" \
  && pass "AC-11: no placeholder pre-empted the real summary (apply-order)" || fail "placeholder leaked before apply"
[ "$(notecount "$T1" "$N1REL")" = "1" ] && pass "AC-1: note link established in task @notes" || fail "note link not established"
[ ! -f "$CF" ] && pass "AC-11: sidecar consumed (unlinked) after apply" || fail "sidecar not consumed"
echo "$O1B" | grep -q "linked note: $N1REL -> $T1BASE" \
  && pass "AC-8: F5 linked-note observability surfaced" || fail "no linked-note F5: $O1B"

# AC-11 idempotency: a further Stop with no sidecar produces no duplicates.
O1C=$(stop 999)
[ "$(sidlines "$T1")" = "1" ] && pass "AC-11: re-run leaves exactly one [s:] line (idempotent)" || fail "duplicate @log line"
[ "$(notecount "$T1" "$N1REL")" = "1" ] && pass "AC-11: re-run leaves exactly one note link (idempotent)" || fail "duplicate note link"

# =====================================================================
# AC-10: expiry (forced 0) — G placeholder + referenced over-bind; unlinked
# note NOT established under judgment-absent expiry.
# =====================================================================
echo ""
echo "[AC-10] expiry: placeholder binds touched task, referenced over-binds known owner, unlinked note not established"
reset_state
A10="$PDIR/tasks/1_in_progress/2026-07-01_ac10a.md"; mk "$A10"
NX="$PDIR/project-notes/specs/ac10-x.md"; printf '# x\n' > "$NX"; NXREL="project-notes/specs/ac10-x.md"
NY="$PDIR/project-notes/specs/ac10-y.md"; printf '# y\n' > "$NY"; NYREL="project-notes/specs/ac10-y.md"
# B10 is the KNOWN owner of NX (pre-linked in its @notes) — drives the reverse index.
B10="$PDIR/tasks/1_in_progress/2026-07-01_ac10b.md"; mk_linked "$B10" "$NXREL"
write_touched "$A10"; write_touched "$NX"; write_touched "$NY"

O10A=$(stop 0)
echo "$O10A" | grep -q '"decision": *"block"' \
  && pass "Stop#1 requests capture (A10 missing + NY unlinked)" || fail "Stop#1 no request: $O10A"
O10B=$(stop 0)
[ "$(sidlines "$A10")" = "1" ] && pass "AC-10: expired → A10 placeholder-bound (G backstop)" || fail "A10 not placeholdered: $(sidlines "$A10")"
# D1 §1.5: placeholder / mechanical notes carry an `(r{N})` round tag so each
# round gets its own idempotency key. This is round 1 of the session.
grep -qF "[s:$SID8]: (auto) touched; summary pending (r1)" "$A10" \
  && pass "AC-10: A10 carries the placeholder provenance with its round tag" \
  || fail "A10 placeholder note wrong: $(grep -F "[s:$SID8]" "$A10")"
[ "$(sidlines "$B10")" = "1" ] && pass "AC-10: NX owner B10 referenced over-bound (reverse-index hit)" || fail "B10 not referenced: $(sidlines "$B10")"
grep -qF "[s:$SID8]: (referenced) owner of $NXREL via reverse-index; capture expired (r1)" "$B10" \
  && pass "AC-10: B10 carries the referenced provenance + round tag (AC-6 識別可能)" \
  || fail "B10 referenced note wrong: $(grep -F "[s:$SID8]" "$B10")"
[ "$(notecount "$A10" "$NYREL")" = "0" ] && pass "AC-10: unlinked note NY NOT established on expiry (judgment-absent)" || fail "NY wrongly established"
[ "$(notecount "$B10" "$NXREL")" = "1" ] && pass "AC-10: pre-existing NX link unchanged (no duplicate)" || fail "NX link disturbed"
echo "$O10B" | grep -q "auto-bound: .*ac10b.md \[s:$SID8\]" \
  && pass "AC-10: referenced over-bind surfaced in F5" || fail "no referenced F5: $O10B"

echo ""
echo "=== Done ==="
