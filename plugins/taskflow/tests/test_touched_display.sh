#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/session_progress_capture.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }

TMP="$(mktemp -d)" || { echo "ABORT: mktemp -d failed" >&2; exit 2; }
[ -n "$TMP" ] && [ -d "$TMP" ] || { echo "ABORT: mktemp -d yielded no usable dir" >&2; exit 2; }
case "$TMP" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ABORT: temp workspace is inside the repo tree" >&2
    rm -rf "$TMP"
    exit 2
    ;;
esac
d="$TMP"
while :; do
  if [ -d "$d/_projects/_state" ]; then
    echo "ABORT: ancestor holds _projects/_state" >&2
    rm -rf "$TMP"
    exit 2
  fi
  p="$(dirname "$d")"
  [ "$p" = "$d" ] && break
  d="$p"
done
cd "$TMP" || { echo "ABORT: cannot enter temp workspace" >&2; rm -rf "$TMP"; exit 2; }

PROJECTS="$TMP/_projects"
STATE="$PROJECTS/_state"
PROJ="_test-touched-display-$$"
PDIR="$PROJECTS/$PROJ"
SID="d15a1a7e-0000-4000-8000-000000000001"
SID8="${SID:0:8}"
SF="$STATE/$SID.json"
TF="$STATE/$SID.touched"
BF="$STATE/$SID.bind"

cleanup() {
  LEAKED=""
  for f in "$REAL_STATE_DIR/$SID".*; do
    [ -e "$f" ] && LEAKED="$LEAKED $(basename "$f")"
  done
  if [ -n "$LEAKED" ]; then
    fail "real _projects/_state/ received a synthetic full SID artifact:$LEAKED"
  else
    pass "real _projects/_state/ has no synthetic full SID artifact"
  fi
  REAL_STATE_AFTER=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
  if [ "$REAL_STATE_BEFORE" = "$REAL_STATE_AFTER" ]; then
    pass "real _projects/_state/ file count is unchanged"
  else
    fail "real _projects/_state/ file count changed"
  fi
  cd "$REPO_ROOT" || true
  rm -rf "$TMP"
  echo ""
  if [ "$FAIL" -eq 0 ]; then
    echo "All $PASS checks passed."
  else
    echo "$FAIL failed, $PASS passed."
    exit 1
  fi
}
trap cleanup EXIT

mkdir -p "$PDIR/tasks/1_in_progress" "$PDIR/project-notes/specs" "$STATE"
printf '{"session_id":"%s","project":"%s"}\n' "$SID" "$PROJ" > "$SF"
printf '# index\n' > "$PDIR/project-notes/index.md"

mk_task() {
  cat > "$1" <<'EOF'
---
priority: LOW
---

# Touched display fixture

<!-- @log:begin -->
- 2026-08-24: created
<!-- @log:end -->
EOF
}

reset_state() { rm -f "$TF" "$BF" "$STATE/$SID".r*.capture; }
write_entry() { printf '%s\n' "$1" >> "$TF"; }
stop() {
  TASKFLOW_LAM="${2:-}" TASKFLOW_SID="$SID" \
    uv run --no-project python -c 'import json,os,sys;p={"session_id":os.environ["TASKFLOW_SID"]};m=os.environ.get("TASKFLOW_LAM","");p.update({"last_assistant_message":m} if m else {});sys.stdout.write(json.dumps(p))' \
    | TASKFLOW_CAPTURE_EXPIRY_S="$1" uv run --no-project python "$(to_win "$HOOK")"
}
reason() { printf '%s' "$1" | uv run --no-project python -c 'import json,sys;sys.stdout.write(json.load(sys.stdin)["reason"])'; }
decision() { printf '%s' "$1" | uv run --no-project python -c 'import json,sys;print(json.load(sys.stdin)["decision"])'; }
display_line() { reason "$1" | uv run --no-project python -c 'import sys;print(next(line for line in sys.stdin if line.startswith("round ledger entries")),end="")'; }
context_json() { reason "$1" | uv run --no-project python -c 'import sys;print(next(line.strip() for line in sys.stdin if line.lstrip().startswith("{\"sid8\"")))'; }
context_tasks() { context_json "$1" | uv run --no-project python -c 'import json,sys;print(",".join(json.load(sys.stdin)["touched_tasks"]))'; }
context_task_count() { context_json "$1" | uv run --no-project python -c 'import json,sys;print(len(json.load(sys.stdin)["touched_tasks"]))'; }
bindq() { uv run --no-project python - "$BF" "$1" <<'PY'
import json, sys
c = json.load(open(sys.argv[1], encoding="utf-8")).get("capture") or {}
print(eval(sys.argv[2]))
PY
}
sid_lines() { grep -cF "[s:$SID8]" "$1" || true; }

printf '=== touched display contract ===\n'

printf '\n--- unclassified round ledger entries stay outside classified task keys ---\n'
reset_state
TASK1="$PDIR/tasks/1_in_progress/2026-08-24_display.md"
mk_task "$TASK1"
ENTRY1="_projects/$PROJ/tasks/1_in_progress/2026-08-24_display.md"
write_entry "$ENTRY1"
write_entry "command"
write_entry "/"
write_entry "$ENTRY1"
OUT1=$(stop 999)
DISPLAY1=$(display_line "$OUT1")
EXPECTED1="round ledger entries (unclassified; diagnostic only): $ENTRY1 command /"
[ "$DISPLAY1" = "$EXPECTED1" ] && pass "display keeps first-observed ledger order and removes duplicates" || fail "display was '$DISPLAY1'"
if reason "$OUT1" | grep -qF 'touched:'; then fail "old touched label remains"; else pass "old touched label is absent"; fi
[ "$(decision "$OUT1")" = "block" ] && pass "valid task ledger entry opens a capture request" || fail "decision was $(decision "$OUT1")"
[ "$(bindq '",".join((c.get("items") or {}).get("tasks") or [])')" = "$PROJ/2026-08-24_display.md" ] && pass "items.tasks excludes diagnostic-only entries" || fail "items.tasks was $(bindq '",".join((c.get("items") or {}).get("tasks") or [])')"
[ "$(context_tasks "$OUT1")" = "$PROJ/2026-08-24_display.md" ] && pass "touched_tasks excludes diagnostic-only entries" || fail "touched_tasks was $(context_tasks "$OUT1")"

printf '\n--- display cap does not cap the classified task context ---\n'
reset_state
for n in $(seq -w 1 31); do
  name="2026-08-24_overflow-$n.md"
  mk_task "$PDIR/tasks/1_in_progress/$name"
  write_entry "_projects/$PROJ/tasks/1_in_progress/$name"
done
OUT2=$(stop 999)
DISPLAY2=$(display_line "$OUT2")
FIRST2="_projects/$PROJ/tasks/1_in_progress/2026-08-24_overflow-01.md"
LAST2="_projects/$PROJ/tasks/1_in_progress/2026-08-24_overflow-31.md"
printf '%s' "$DISPLAY2" | grep -qF "$FIRST2" && pass "display includes the first capped entry" || fail "display omits the first capped entry"
printf '%s' "$DISPLAY2" | grep -qF ' ...(1 more)' && pass "display preserves the overflow suffix" || fail "display lacks the overflow suffix"
if printf '%s' "$DISPLAY2" | grep -qF "$LAST2"; then fail "display includes the overflow entry"; else pass "display omits the overflow entry"; fi
[ "$(context_task_count "$OUT2")" = "31" ] && pass "touched_tasks keeps every classified task beyond the display cap" || fail "touched_tasks count was $(context_task_count "$OUT2")"

printf '\n--- empty ledger display can accompany an exec-carry capture request ---\n'
reset_state
TASK3="$PDIR/tasks/1_in_progress/2026-08-24_exec-carry.md"
mk_task "$TASK3"
CARRY="[tasks: 2026-08-24_exec-carry.md]"
OUT3A=$(stop 999 "$CARRY")
[ "$(sid_lines "$TASK3")" = "1" ] && pass "first carry creates the deterministic same-SID binding" || fail "same-SID line count was $(sid_lines "$TASK3")"
[ "$(bindq 'c.get("round")')" = "0" ] && pass "first carry does not open a capture round" || fail "first carry round was $(bindq 'c.get("round")')"
[ "$(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-24_exec-carry.md')")" = "1" ] && pass "first carry synchronizes log_seen to the deterministic binding" || fail "first carry log_seen was $(bindq "(c.get('log_seen') or {}).get('$PROJ/2026-08-24_exec-carry.md')")"
OUT3B=$(stop 999 "$CARRY")
[ "$(decision "$OUT3B")" = "block" ] && pass "second carry opens a capture request" || fail "second carry decision was $(decision "$OUT3B")"
[ "$(display_line "$OUT3B")" = "round ledger entries (unclassified; diagnostic only): (none)" ] && pass "second carry displays an empty ledger diagnostic" || fail "second carry display was $(display_line "$OUT3B")"
[ "$(context_tasks "$OUT3B")" = "$PROJ/2026-08-24_exec-carry.md" ] && pass "second carry reaches touched_tasks without a ledger entry" || fail "second carry touched_tasks was $(context_tasks "$OUT3B")"
[ "$(bindq 'c.get("round")')" = "1" ] && pass "second carry advances the capture round" || fail "second carry round was $(bindq 'c.get("round")')"

if [ "$FAIL" -ne 0 ]; then exit 1; fi
