#!/usr/bin/env bash
# test_round_binding.sh — T-D1-1..T-D1-5: round-based binding in the Stop hook
# (project-notes/specs/capture-detection-gaps.md §1 / D1).
#
# Before D1 the ledger was binary — `log_block_has_sid` — so the FIRST bind of a
# task removed it from detection for the rest of the session and every later
# round's work was dropped in silence. D1 replaces it with a cursor over the
# append-only `.touched` ledger plus a per-round `[s:sid8]` line count:
#
#   T-D1-1  the same session binds the same task once per ROUND (r1 / r2)
#   T-D1-2  a round the agent logged itself spawns nothing and places no
#           placeholder — only the cursor / log_seen advance (§1.4)
#   T-D1-3  M-1 bootstrap: a `.bind` with no `touch_cursor` starts at the END of
#           the ledger, so upgrading does not replay history — and a round still
#           forms from activity that happens AFTER the upgrade (§1.8)
#   T-D1-4  text-key idempotency: re-applying the identical capture summary
#           (the sidecar-unlink-failed path) adds no second line (§1.5)
#   T-D1-5  a note write with a known owner puts that OWNER in A_r — the
#           via-a-note path that produced the real 2026-08 loss (§1.3)
#
# State-dir sandbox (plugins/taskflow/CLAUDE.md `e2e_state_dir_sandbox`): the
# Stop hook runs an unconditional stale-marker sweep on every invocation and
# resolves `_projects` via getcwd() (no env override). This test therefore `cd`s
# into an isolated tempdir and builds `_projects/` there — it NEVER cd's into
# $REPO_ROOT while invoking the hook, so the sweep can never reach the real
# _projects/_state/ (2026-07-17 incident: a wrong-cwd run deleted 250 real
# session-state files there). The real dir's file count is bracketed below.
#
# Usage:  bash plugins/taskflow/tests/test_round_binding.sh
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
PROJ="_test-round-$$"
PDIR="$PROJECTS/$PROJ"
SID="rndbind$$-0000-0000-0000-000000000000"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"; TF="$STATE/$SID.touched"; BF="$STATE/$SID.bind"; CF="$STATE/$SID.capture"
# rcap(): per-round sidecar path (F-2 migration; absolute — suite has cd'd to $TMP)
. "$REPO_ROOT/plugins/taskflow/tests/capture_paths.sh"

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

reset_state() { rm -f "$TF" "$BF" "$CF" "$STATE/$SID".r*.capture; }

mk() {  # $1 = task md path (with an @log block, no @notes)
  cat > "$1" << 'T'
---
priority: HIGH
---

# Round-binding test task

## Next Steps
- (none)

<!-- @log:begin -->
- 2026-08-09: created
<!-- @log:end -->
T
}

mk_linked() {  # $1 = task md path, $2 = note project-rel pre-linked in @notes
  cat > "$1" << T
---
priority: HIGH
---

# Round-binding note-owner task

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

stop() {  # $1 = expiry seconds; $2 = optional last_assistant_message
  export TASKFLOW_CAPTURE_EXPIRY_S="$1"
  TASKFLOW_LAM="${2:-}" TASKFLOW_SID="$SID" \
    uv run --no-project python -c "import json,os,sys;p={'session_id':os.environ['TASKFLOW_SID']};lam=os.environ.get('TASKFLOW_LAM','');p.update({'last_assistant_message':lam} if lam else {});sys.stdout.write(json.dumps(p))" \
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
line = "- 2026-08-09T10:00:00+09:00 [s:%s]: %s\n" % (sid8, note)
open(path, "w", encoding="utf-8").write(c[:at] + line + c[at:])
PY
}

echo "=== T-D1-1..5: round-based binding (capture-detection-gaps.md §1 / D1) ==="
echo "  project=$PROJ  sid8=$SID8  (isolated tempdir: $TMP)"
echo ""

# =====================================================================
# T-D1-1: one session, two rounds -> TWO @log lines (r1 and r2).
# Pre-D1 the second round produced nothing at all: `log_block_has_sid`
# removed the task from `missing` forever after the first bind.
# =====================================================================
echo "[T-D1-1] two rounds in one session -> two placeholder lines (r1 / r2)"
reset_state
T1="$PDIR/tasks/1_in_progress/2026-08-09_round1.md"; mk "$T1"
write_touched "$T1"
stop 0 >/dev/null            # Stop#1: round 1 requested
O12=$(stop 0)                # Stop#2: expiry -> r1 placeholder
[ "$(sidlines "$T1")" = "1" ] \
  && pass "round 1 bound exactly one line" || fail "round 1 lines: $(sidlines "$T1")"
grep -qF "[s:$SID8]: (auto) touched; summary pending (r1)" "$T1" \
  && pass "round 1 placeholder carries the (r1) round tag" \
  || fail "round 1 note wrong: $(grep -F "[s:$SID8]" "$T1")"
echo "$O12" | grep -q "auto-bound: .*2026-08-09_round1.md" \
  && pass "round 1 backstop reported on the block channel" || fail "no r1 F5: $O12"

write_touched "$T1"          # round 2: a NEW write event on the same task
O13=$(stop 0)
echo "$O13" | grep -q '"decision": *"block"' \
  && pass "round 2 forms and requests capture (the pre-D1 silent-loss case)" \
  || fail "round 2 did not form: $O13"
[ "$(bindq 'c.get("round")')" = "2" ] \
  && pass ".bind capture.round advanced to 2" || fail "round counter: $(bindq 'c.get("round")')"
stop 0 >/dev/null            # Stop#4: expiry -> r2 placeholder
[ "$(sidlines "$T1")" = "2" ] \
  && pass "round 2 appended a SECOND [s:$SID8] line (2 total)" \
  || fail "round 2 line count: $(sidlines "$T1")"
grep -qF "[s:$SID8]: (auto) touched; summary pending (r2)" "$T1" \
  && pass "round 2 placeholder carries the (r2) round tag (distinct text key)" \
  || fail "round 2 note wrong: $(grep -F "[s:$SID8]" "$T1")"

# =====================================================================
# T-D1-2: a round the agent logged itself spawns nothing (§1.4) — the
# structural answer to "the hook must not spawn a subagent every turn".
# =====================================================================
echo ""
echo "[T-D1-2] self-logged round: no spawn, no placeholder, cursor still advances"
reset_state
T2="$PDIR/tasks/1_in_progress/2026-08-09_selflog.md"; mk "$T2"
write_touched "$T2"
agent_log_line "$T2" "agent wrote its own progress line"
O21=$(stop 0)
if [ -z "$O21" ] || ! echo "$O21" | grep -q '"decision": *"block"'; then
  pass "self-logged round does NOT block (no capture spawn)"
else
  fail "self-logged round spawned/blocked: $O21"
fi
[ "$(sidlines "$T2")" = "1" ] \
  && pass "no placeholder added on top of the agent's own line" \
  || fail "placeholder leaked: $(sidlines "$T2")"
[ "$(bindq 'c.get("touch_cursor")')" = "1" ] \
  && pass "touch_cursor consumed the slice (1)" || fail "cursor: $(bindq 'c.get("touch_cursor")')"
# D2 (§3.3): round keys are QUALIFIED `<project>/<basename>`.
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_selflog.md')")" = "1" ] \
  && pass "log_seen recorded the agent's line (1), keyed by project/basename" \
  || fail "log_seen: $(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-09_selflog.md')")"
[ "$(bindq 'c.get("round")')" = "0" ] \
  && pass "no round was opened (round still 0)" || fail "round: $(bindq 'c.get("round")')"
O22=$(stop 0)
if [ -z "$O22" ] || ! echo "$O22" | grep -q '"decision": *"block"'; then
  pass "still silent on the next Stop (stable, no loop)"
else
  fail "second Stop blocked: $O22"
fi

# =====================================================================
# T-D1-3: M-1 bootstrap — a `.bind` with no touch_cursor must NOT replay
# the ledger history it was written before (upgrade storm), but must still
# form rounds from activity that happens after the upgrade.
# =====================================================================
echo ""
echo "[T-D1-3] M-1 bootstrap: pre-D1 .bind does not replay history"
reset_state
T3="$PDIR/tasks/1_in_progress/2026-08-09_migrate.md"; mk "$T3"
write_touched "$T3"; write_touched "$T3"; write_touched "$T3"   # 3 pre-upgrade events
# A `.bind` in the pre-D1 shape: capture lifecycle present, no round keys.
cat > "$BF" << 'EOF'
{"reminded":{},"exec_tried":[],"capture":{"status":"","items":null,"requested_ts":null,"tried_notes":[],"tried_tasks":[]}}
EOF
O31=$(stop 0)
if [ -z "$O31" ] || ! echo "$O31" | grep -q '"decision": *"block"'; then
  pass "upgrade Stop forms NO round from the pre-existing ledger (no storm)"
else
  fail "upgrade Stop replayed history: $O31"
fi
[ "$(sidlines "$T3")" = "0" ] \
  && pass "no line written for the replayed history" || fail "history was re-captured: $(sidlines "$T3")"
[ "$(bindq 'c.get("touch_cursor")')" = "3" ] \
  && pass "touch_cursor bootstrapped to the ledger end (3)" \
  || fail "bootstrap cursor: $(bindq 'c.get("touch_cursor")')"
[ "$(bindq 'c.get("round")')" = "0" ] \
  && pass "no round formed by the bootstrap (round 0)" || fail "round: $(bindq 'c.get("round")')"
write_touched "$T3"          # post-upgrade activity
O32=$(stop 0)
echo "$O32" | grep -q '"decision": *"block"' \
  && pass "post-upgrade activity DOES form a round (bootstrap is not a mute)" \
  || fail "post-upgrade round missing: $O32"
[ "$(bindq 'c.get("round")')" = "1" ] \
  && pass "first post-upgrade round is r1" || fail "round: $(bindq 'c.get("round")')"

# =====================================================================
# T-D1-4: text-key idempotency (§1.5) — the sidecar-unlink-failed path
# re-applies the SAME summary on a later Stop and must add no second line.
# =====================================================================
echo ""
echo "[T-D1-4] re-applying the identical capture summary adds no duplicate"
reset_state
T4="$PDIR/tasks/1_in_progress/2026-08-09_reapply.md"; mk "$T4"
write_touched "$T4"
stop 999 >/dev/null          # request round 1 (no expiry: backstop must not fire)
cat > "$(rcap 1)" << 'EOF'
{"confirmed":[{"task":"2026-08-09_reapply.md","summary":"REAPPLYSUMMARY"}],"note_links":[],"proposals":[]}
EOF
O41=$(stop 999)
[ "$(sidlines "$T4")" = "1" ] \
  && pass "the capture summary applied once" || fail "apply count: $(sidlines "$T4")"
echo "$O41" | grep -q "applied summary: $PROJ/2026-08-09_reapply.md" \
  && pass "first apply is reported once" || fail "no applied-summary F5: $O41"
# Simulate `os.remove(capture_path)` failing: the sidecar survives and the
# lifecycle stays `requested`, so the NEXT Stop re-enters the apply branch.
# The `items` / `round_base` keys written below are deliberately left BARE
# (pre-D2 shape): the F-4 migration path (§3.4) must read them as the primary
# project's qualified keys, so this fixture doubles as a legacy-key check.
cat > "$(rcap 1)" << 'EOF'
{"confirmed":[{"task":"2026-08-09_reapply.md","summary":"REAPPLYSUMMARY"}],"note_links":[],"proposals":[]}
EOF
uv run --no-project python - "$BF" << 'PY'
import json, sys, time
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
c = d["capture"]
c["status"] = "requested"          # unlink failed -> lifecycle never advanced
c["requested_ts"] = time.time()
c["items"] = {"tasks": ["2026-08-09_reapply.md"], "notes": []}
c["round_base"] = {"2026-08-09_reapply.md": 0}
# F-2: drop `history` so the r1 apply is gated on the items_open FALLBACK —
# the bare keys above are the point of this fixture (§3.4 legacy-key read),
# and history (written qualified by the real request commit) would mask them.
c.pop("history", None)
json.dump(d, open(p, "w", encoding="utf-8"), ensure_ascii=False)
PY
O42=$(stop 999)
[ "$(sidlines "$T4")" = "1" ] \
  && pass "re-apply of the identical summary left exactly one line (text key)" \
  || fail "duplicate summary line: $(sidlines "$T4")"
[ "$(grep -cF 'REAPPLYSUMMARY' "$T4")" = "1" ] \
  && pass "the summary text appears exactly once in the task md" \
  || fail "summary text occurrences: $(grep -cF 'REAPPLYSUMMARY' "$T4")"
echo "$O42" | grep -q "applied summary: $PROJ/2026-08-09_reapply.md" \
  && fail "the no-op re-apply was reported as an applied summary: $O42" \
  || pass "the no-op re-apply is not re-reported (INV-1 boundedness)"

# =====================================================================
# T-D1-5: a note write whose owner is known via the reverse index puts that
# OWNER into A_r (§1.3). Pre-D1 nothing here entered detection at all: the
# task md was never written, and the note is already linked so it was not
# `unlinked` either — the exact via-a-note loss path.
# =====================================================================
echo ""
echo "[T-D1-5] note write -> reverse-index owner enters A_r"
reset_state
NOTE="$PDIR/project-notes/specs/round-owner.md"; printf '# owner note\n' > "$NOTE"
NOTEREL="project-notes/specs/round-owner.md"
T5="$PDIR/tasks/1_in_progress/2026-08-09_note-owner.md"; mk_linked "$T5" "$NOTEREL"
write_touched "$NOTE"        # ONLY the note is written this round
O51=$(stop 999)
echo "$O51" | grep -q '"decision": *"block"' \
  && pass "a note-only round still requests a capture" || fail "no request: $O51"
[ "$(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')" = "$PROJ/2026-08-09_note-owner.md" ] \
  && pass "the note's owning task is in the round's closed item set (qualified)" \
  || fail "items.tasks: $(bindq '",".join(((c.get("items") or {}).get("tasks") or []))')"
echo "$O51" | grep -qF "2026-08-09_note-owner.md" \
  && pass "the owner is named in the capture context handed to the subagent" \
  || fail "owner missing from the spawn context: $O51"
[ "$(sidlines "$T5")" = "0" ] \
  && pass "the owner is not bound yet (request only, no premature placeholder)" \
  || fail "premature bind: $(sidlines "$T5")"

echo ""
echo "=== Done ==="
