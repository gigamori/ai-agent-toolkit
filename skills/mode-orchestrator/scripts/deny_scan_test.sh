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

echo
echo "passed $PASS / $N"
[ "$FAIL" -eq 0 ]
