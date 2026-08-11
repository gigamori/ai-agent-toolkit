#!/usr/bin/env bash
# Self-tests for deny_scan.sh.
#
#   bash scripts/deny_scan_test.sh
#
# Same discipline as watchdog_test.sh: every test builds its own fake project
# root under a temp dir; nothing touches a real session log or needs the
# claude CLI. Transcript lines mirror the measured shape of the defect-4
# incident: a denial is a single JSONL line carrying both "is_error":true and
# the denial phrase.

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
SCAN="$HERE/deny_scan.sh"

[ -f "$SCAN" ] || { echo "deny_scan.sh not found next to this script" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
N=0

DS_OUT=""
DS_RC=0

run_ds() {
  DS_OUT="$(bash "$SCAN" "$@" 2>/dev/null)"
  DS_RC=$?
}

expect() {
  local name="$1" ew="$2" erc="$3"
  N=$((N + 1))
  if [ "$DS_OUT" = "$ew" ] && [ "$DS_RC" = "$erc" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %2d  %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %2d  %s — expected [%s]/%s, got [%s]/%s\n' \
      "$N" "$name" "$ew" "$erc" "$DS_OUT" "$DS_RC"
  fi
}

# mk_agent <root> <session> <id> <desc>  — writes meta.json, echoes transcript path
mk_agent() {
  local root="$1" session="$2" id="$3" desc="$4"
  local dir="$root/proj/$session/subagents"
  mkdir -p "$dir"
  printf '{"agentType":"general-purpose","description":"%s"}' "$desc" \
    > "$dir/agent-$id.meta.json"
  echo "$dir/agent-$id.jsonl"
}

DENIAL_CLASSIFIER='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"Permission for this action was denied by the Claude Code auto mode classifier. Reason: Blocked."}]}}'
DENIAL_MANUAL="{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"is_error\":true,\"content\":\"The user doesn't want to proceed with this tool use.\"}]}}"
# Current template family, transcribed from two real transcripts measured
# 2026-08-12 (decision-point E2E, sessions 72f92585 turn b3-turn2-execute and
# 88ae5b3d turn "b8 turn1 execute probe hostname"). Both were real Bash
# denials that the two older patterns missed completely. Keep the shape —
# including the trailing `toolUseResult` field, which is where the phrase
# actually lands — so this asserts against the real serialization rather than
# a tidied-up version of it.
DENIAL_TOOL_PLAIN='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_x"}]},"toolUseResult":"Error: Permission to use Bash has been denied. IMPORTANT: You *may* attempt to accomplish this action using other tools."}'
DENIAL_TOOL_WITHCMD='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_y"}]},"toolUseResult":"Error: Permission to use Bash with command hostname has been denied."}'
# A non-denial tool error on the same is_error line must stay CLEAN — this is
# what keeps the new, shorter pattern from turning every failed command into a
# permission denial.
TOOL_ERROR='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_z"}]},"toolUseResult":"Error: Exit code 2\nls: cannot access: No such file or directory"}'
OK_RESULT='{"type":"user","message":{"content":[{"type":"tool_result","is_error":false,"content":"file written"}]}}'
ASSISTANT='{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
# The phrase appearing WITHOUT is_error:true (e.g. the agent read a doc that
# quotes it) must not trip the scan.
QUOTED_ONLY='{"type":"user","message":{"content":[{"type":"tool_result","is_error":false,"content":"the doc says: Permission for this action was denied means a policy block"}]}}'

# --- 1. clean transcript -> CLEAN/0
R="$WORK/t1"
T="$(mk_agent "$R" sess1 aaa 'turn one desc')"
printf '%s\n%s\n' "$ASSISTANT" "$OK_RESULT" > "$T"
run_ds --desc 'turn one desc' --project-root "$R"
expect "clean transcript" "CLEAN" 0

# --- 2. classifier denial -> DENIED 1/1
R="$WORK/t2"
T="$(mk_agent "$R" sess1 bbb 'turn two desc')"
printf '%s\n%s\n%s\n' "$ASSISTANT" "$DENIAL_CLASSIFIER" "$OK_RESULT" > "$T"
run_ds --desc 'turn two desc' --project-root "$R"
expect "classifier denial detected" "DENIED 1" 1

# --- 3. manual-deny wording -> DENIED 1/1
R="$WORK/t3"
T="$(mk_agent "$R" sess1 ccc 'turn three desc')"
printf '%s\n' "$DENIAL_MANUAL" > "$T"
run_ds --desc 'turn three desc' --project-root "$R"
expect "manual denial detected" "DENIED 1" 1

# --- 4. two denials counted
R="$WORK/t4"
T="$(mk_agent "$R" sess1 ddd 'turn four desc')"
printf '%s\n%s\n' "$DENIAL_CLASSIFIER" "$DENIAL_CLASSIFIER" > "$T"
run_ds --desc 'turn four desc' --project-root "$R"
expect "two denials counted" "DENIED 2" 1

# --- 4b. current template family (measured 2026-08-12) -> DENIED
R="$WORK/t4b"
T="$(mk_agent "$R" sess1 ddb 'turn four-b desc')"
printf '%s\n%s\n' "$ASSISTANT" "$DENIAL_TOOL_PLAIN" > "$T"
run_ds --desc 'turn four-b desc' --project-root "$R"
expect "current tool-denial wording detected" "DENIED 1" 1

# --- 4c. same family with the " with command <cmd>" clause -> DENIED
R="$WORK/t4c"
T="$(mk_agent "$R" sess1 ddc 'turn four-c desc')"
printf '%s\n' "$DENIAL_TOOL_WITHCMD" > "$T"
run_ds --desc 'turn four-c desc' --project-root "$R"
expect "tool-denial with command clause detected" "DENIED 1" 1

# --- 4d. an ordinary tool error is not a denial -> CLEAN
R="$WORK/t4d"
T="$(mk_agent "$R" sess1 ddd2 'turn four-d desc')"
printf '%s\n%s\n' "$ASSISTANT" "$TOOL_ERROR" > "$T"
run_ds --desc 'turn four-d desc' --project-root "$R"
expect "non-denial tool error stays clean" "CLEAN" 0

# --- 5. phrase without is_error:true does not trip
R="$WORK/t5"
T="$(mk_agent "$R" sess1 eee 'turn five desc')"
printf '%s\n' "$QUOTED_ONLY" > "$T"
run_ds --desc 'turn five desc' --project-root "$R"
expect "quoted phrase without is_error is clean" "CLEAN" 0

# --- 6. unresolvable description -> NO-TRANSCRIPT/2
R="$WORK/t6"
mkdir -p "$R/proj"
run_ds --desc 'no such turn' --project-root "$R"
expect "unresolvable desc fails visible" "NO-TRANSCRIPT" 2

# --- 7. description must match exactly (prefix of another turn's does not collide)
R="$WORK/t7"
T="$(mk_agent "$R" sess1 fff 'turn seven desc (re-run)')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T"
run_ds --desc 'turn seven desc' --project-root "$R"
expect "prefix desc does not collide" "NO-TRANSCRIPT" 2

# --- 8. meta resolved but transcript file missing -> NO-TRANSCRIPT/2
R="$WORK/t8"
T="$(mk_agent "$R" sess1 ggg 'turn eight desc')"
# no transcript written
run_ds --desc 'turn eight desc' --project-root "$R"
expect "meta without transcript fails visible" "NO-TRANSCRIPT" 2

# --- 9. duplicate description (contract violation): newest meta wins.
# Exercises the portable mtime path (stat -c / stat -f fallback): the stale
# duplicate holds a denial, the newest is clean -> CLEAN proves selection.
R="$WORK/t9"
T_OLD="$(mk_agent "$R" sessOld hhh 'turn nine desc')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T_OLD"
touch -d '2001-01-01 00:00:00' "${T_OLD%.jsonl}.meta.json" 2>/dev/null \
  || touch -t 200101010000 "${T_OLD%.jsonl}.meta.json"
T_NEW="$(mk_agent "$R" sessNew iii 'turn nine desc')"
printf '%s\n' "$OK_RESULT" > "$T_NEW"
run_ds --desc 'turn nine desc' --project-root "$R"
expect "newest meta wins on duplicate desc" "CLEAN" 0

echo
echo "passed $PASS / $N"
[ "$FAIL" -eq 0 ]
