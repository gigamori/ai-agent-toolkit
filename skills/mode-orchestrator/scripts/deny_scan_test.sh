#!/usr/bin/env bash

set -u
unset CLAUDE_CODE_SESSION_ID

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

bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 10 "$@"
  else
    "$@"
  fi
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
DENIAL_TOOL_PLAIN='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_x"}]},"toolUseResult":"Error: Permission to use Bash has been denied. IMPORTANT: You *may* attempt to accomplish this action using other tools."}'
DENIAL_TOOL_WITHCMD='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_y"}]},"toolUseResult":"Error: Permission to use Bash with command hostname has been denied."}'
TOOL_ERROR='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"tool_use_id":"toolu_z"}]},"toolUseResult":"Error: Exit code 2\nls: cannot access: No such file or directory"}'
OK_RESULT='{"type":"user","message":{"content":[{"type":"tool_result","is_error":false,"content":"file written"}]}}'
ASSISTANT='{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}'
QUOTED_ONLY='{"type":"user","message":{"content":[{"type":"tool_result","is_error":false,"content":"the doc says: Permission for this action was denied means a policy block"}]}}'
DENIAL_HOOK='{"type":"user","message":{"content":[{"type":"tool_result","content":"Blocked by a project hook: take the sanctioned route.","is_error":true,"tool_use_id":"toolu_h"}]},"toolUseResult":"Error: Blocked by a project hook: take the sanctioned route.","toolDenialKind":"permission-rule"}'
DENIAL_CANCELLED="{\"type\":\"user\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"content\":\"The user doesn't want to take this action right now. STOP what you are doing and wait for the user to tell you how to proceed.\",\"is_error\":true,\"tool_use_id\":\"toolu_c\"}]},\"toolDenialKind\":\"cancelled\"}"
DENIAL_UNAVAILABLE='{"type":"user","message":{"content":[{"type":"tool_result","content":"example-model is temporarily unavailable, so auto mode cannot determine the safety of Bash right now.","is_error":true,"tool_use_id":"toolu_u"}]},"toolDenialKind":"automode-unavailable"}'
DENIAL_TWO_PHRASES='{"type":"user","message":{"content":[{"type":"tool_result","is_error":true,"content":"Permission for this action was denied by the Claude Code auto mode classifier. Permission to use Bash has been denied."}]}}'

R="$WORK/t1"
T="$(mk_agent "$R" sess1 aaa 'turn one desc')"
printf '%s\n%s\n' "$ASSISTANT" "$OK_RESULT" > "$T"
run_ds --desc 'turn one desc' --project-root "$R"
expect "clean transcript" "CLEAN" 0

R="$WORK/t2"
T="$(mk_agent "$R" sess1 bbb 'turn two desc')"
printf '%s\n%s\n%s\n' "$ASSISTANT" "$DENIAL_CLASSIFIER" "$OK_RESULT" > "$T"
run_ds --desc 'turn two desc' --project-root "$R"
expect "classifier denial detected" "DENIED 1" 1

R="$WORK/t3"
T="$(mk_agent "$R" sess1 ccc 'turn three desc')"
printf '%s\n' "$DENIAL_MANUAL" > "$T"
run_ds --desc 'turn three desc' --project-root "$R"
expect "manual denial detected" "DENIED 1" 1

R="$WORK/t4"
T="$(mk_agent "$R" sess1 ddd 'turn four desc')"
printf '%s\n%s\n' "$DENIAL_CLASSIFIER" "$DENIAL_CLASSIFIER" > "$T"
run_ds --desc 'turn four desc' --project-root "$R"
expect "two denials counted" "DENIED 2" 1

R="$WORK/t4b"
T="$(mk_agent "$R" sess1 ddb 'turn four-b desc')"
printf '%s\n%s\n' "$ASSISTANT" "$DENIAL_TOOL_PLAIN" > "$T"
run_ds --desc 'turn four-b desc' --project-root "$R"
expect "current tool-denial wording detected" "DENIED 1" 1

R="$WORK/t4c"
T="$(mk_agent "$R" sess1 ddc 'turn four-c desc')"
printf '%s\n' "$DENIAL_TOOL_WITHCMD" > "$T"
run_ds --desc 'turn four-c desc' --project-root "$R"
expect "tool-denial with command clause detected" "DENIED 1" 1

R="$WORK/t4d"
T="$(mk_agent "$R" sess1 ddd2 'turn four-d desc')"
printf '%s\n%s\n' "$ASSISTANT" "$TOOL_ERROR" > "$T"
run_ds --desc 'turn four-d desc' --project-root "$R"
expect "non-denial tool error stays clean" "CLEAN" 0

R="$WORK/t5"
T="$(mk_agent "$R" sess1 eee 'turn five desc')"
printf '%s\n' "$QUOTED_ONLY" > "$T"
run_ds --desc 'turn five desc' --project-root "$R"
expect "quoted phrase without is_error is clean" "CLEAN" 0

R="$WORK/t6"
mkdir -p "$R/proj"
run_ds --desc 'no such turn' --project-root "$R"
expect "unresolvable desc fails visible" "NO-TRANSCRIPT" 2

R="$WORK/t7"
T="$(mk_agent "$R" sess1 fff 'turn seven desc (re-run)')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T"
run_ds --desc 'turn seven desc' --project-root "$R"
expect "prefix desc does not collide" "NO-TRANSCRIPT" 2

R="$WORK/t8"
T="$(mk_agent "$R" sess1 ggg 'turn eight desc')"
run_ds --desc 'turn eight desc' --project-root "$R"
expect "meta without transcript fails visible" "NO-TRANSCRIPT" 2

R="$WORK/t9"
T_OLD="$(mk_agent "$R" sessOld hhh 'turn nine desc')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T_OLD"
touch -d '2001-01-01 00:00:00' "${T_OLD%.jsonl}.meta.json" 2>/dev/null \
  || touch -t 200101010000 "${T_OLD%.jsonl}.meta.json"
T_NEW="$(mk_agent "$R" sessNew iii 'turn nine desc')"
printf '%s\n' "$OK_RESULT" > "$T_NEW"
run_ds --desc 'turn nine desc' --project-root "$R"
expect "newest meta wins on duplicate desc" "CLEAN" 0

R="$WORK/t10"
T="$(mk_agent "$R" sess1 jjj 'turn ten desc')"
printf '%s\n%s\n' "$ASSISTANT" "$DENIAL_HOOK" > "$T"
run_ds --desc 'turn ten desc' --project-root "$R"
expect "hook-worded denial detected by its toolDenialKind" "DENIED 1" 1

R="$WORK/t11"
T="$(mk_agent "$R" sess1 kkk 'turn eleven desc')"
printf '%s\n%s\n' "$DENIAL_CANCELLED" "$DENIAL_UNAVAILABLE" > "$T"
run_ds --desc 'turn eleven desc' --project-root "$R"
expect "cancelled and classifier-unavailable calls count as denials" "DENIED 2" 1

R="$WORK/t12"
T="$(mk_agent "$R" sess1 lll 'turn twelve desc')"
printf '%s\n' "$DENIAL_TWO_PHRASES" > "$T"
run_ds --desc 'turn twelve desc' --project-root "$R"
expect "one record matching two phrases counts once" "DENIED 1" 1

R="$WORK/t13"
T="$(mk_agent "$R" sess1 mmm 'turn thirteen desc')"
: > "$T"
run_ds --desc 'turn thirteen desc' --project-root "$R"
expect "empty transcript fails visible" "NO-TRANSCRIPT" 2

R="$WORK/t14"
T="$(mk_agent "$R" sess1 nnn 'turn fourteen desc')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T"
printf '{"type":"assistant","message":{"content":[{"type":"text","text":"a\0b"}]}}\n' >> "$T"
run_ds --desc 'turn fourteen desc' --project-root "$R"
expect "NUL byte in transcript does not hide a denial" "DENIED 1" 1

R="$WORK/t15"
T="$(mk_agent "$R" sess1 ooo 'turn fifteen desc')"
printf '%s\n' "$OK_RESULT" > "$T"
run_ds --desc 'turn fifteen desc' --project-root "$R" --out "$R/15-deny.txt"
DS_OUT="$(head -n 1 "$R/15-deny.txt" 2>/dev/null)"
expect "--out file starts with the verdict" "CLEAN" 0

R="$WORK/t16"
T="$(mk_agent "$R" sess1 ppp 'turn sixteen desc')"
printf '%s\n' "$DENIAL_HOOK" > "$T"
run_ds --desc 'turn sixteen desc' --project-root "$R" --out "$R/16-deny.txt"
DS_OUT="$(sed -n 's/^transcript: //p' "$R/16-deny.txt" 2>/dev/null)"
expect "--out file names the transcript it scanned" "$T" 1

R="$WORK/t17"
mkdir -p "$R/proj"
run_ds --desc 'no such turn' --project-root "$R" --out "$R/17-deny.txt"
DS_OUT="$(head -n 1 "$R/17-deny.txt" 2>/dev/null)"
expect "--out file records NO-TRANSCRIPT" "NO-TRANSCRIPT" 2

R="$WORK/t18"
T="$(mk_agent "$R" sess1 qqq 'turn eighteen desc')"
printf '%s\n' "$DENIAL_HOOK" > "$T"
run_ds --desc 'turn eighteen desc' --project-root "$R" --out "$R/no-such-dir/18-deny.txt"
expect "unwritable --out keeps the verdict and exit code" "DENIED 1" 1

DS_OUT="$(bounded bash "$SCAN" --desc 2>/dev/null)"
DS_RC=$?
expect "a bare --desc is a usage error, not a hang" "" 64

R="$WORK/t20"
mkdir -p "$R/proj"
DS_OUT="$(bounded bash "$SCAN" --desc x --project-root "$R" --out 2>/dev/null)"
DS_RC=$?
expect "a bare --out is a usage error, not a hang" "" 64

R="$WORK/t21"
T_A="$(mk_agent "$R" sessA aaa2 'shared desc')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T_A"
touch -d '2001-01-01 00:00:00' "${T_A%.jsonl}.meta.json" 2>/dev/null \
  || touch -t 200101010000 "${T_A%.jsonl}.meta.json"
T_B="$(mk_agent "$R" sessB bbb2 'shared desc')"
printf '%s\n' "$OK_RESULT" > "$T_B"
export CLAUDE_CODE_SESSION_ID=sessA
run_ds --desc 'shared desc' --project-root "$R"
unset CLAUDE_CODE_SESSION_ID
expect "the calling session's turn wins over a newer one elsewhere" "DENIED 1" 1

R="$WORK/t22"
T="$(mk_agent "$R" sessB ccc2 'other session only')"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$T"
export CLAUDE_CODE_SESSION_ID=sessA
run_ds --desc 'other session only' --project-root "$R"
unset CLAUDE_CODE_SESSION_ID
expect "a description found only in another session is not this turn" "NO-TRANSCRIPT" 2

export CLAUDE_CODE_SESSION_ID=
run_ds --desc 'other session only' --project-root "$R"
unset CLAUDE_CODE_SESSION_ID
expect "an empty session id searches every session, as the canary needs" "DENIED 1" 1

R="$WORK/t27"
T="$(mk_agent "$R" sess1 ttt 'turn with a child')"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_child1","name":"Agent","input":{}}]}}' > "$T"
D="$R/proj/sess1/subagents"
printf '{"agentType":"general-purpose","description":"child","toolUseId":"toolu_child1","spawnDepth":2}' > "$D/agent-kid1.meta.json"
printf '%s\n' "$DENIAL_HOOK" > "$D/agent-kid1.jsonl"
run_ds --desc 'turn with a child' --project-root "$R"
expect "a denial inside an agent the turn started counts" "DENIED 1" 1

R="$WORK/t28"
T="$(mk_agent "$R" sess1 uuu 'turn with a skill child')"
printf '%s\n' "$ASSISTANT" > "$T"
D="$R/proj/sess1/subagents"
printf '{"agentType":"general-purpose","parentAgentId":"uuu","spawnDepth":2}' > "$D/agent-kid2.meta.json"
printf '%s\n' "$DENIAL_HOOK" > "$D/agent-kid2.jsonl"
run_ds --desc 'turn with a skill child' --project-root "$R"
expect "a denial inside a child that names the turn as its parent agent counts" "DENIED 1" 1

R="$WORK/t29"
T="$(mk_agent "$R" sess1 vvv 'turn with a grandchild')"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_c3","name":"Agent","input":{}}]}}' > "$T"
D="$R/proj/sess1/subagents"
printf '{"agentType":"general-purpose","description":"c","toolUseId":"toolu_c3","spawnDepth":2}' > "$D/agent-kid3.meta.json"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_g3","name":"Agent","input":{}}]}}' > "$D/agent-kid3.jsonl"
printf '{"agentType":"general-purpose","description":"g","toolUseId":"toolu_g3","spawnDepth":3}' > "$D/agent-grand3.meta.json"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$D/agent-grand3.jsonl"
run_ds --desc 'turn with a grandchild' --project-root "$R"
expect "a denial two levels below the turn counts" "DENIED 1" 1

R="$WORK/t30"
T="$(mk_agent "$R" sess1 www 'turn without children')"
printf '%s\n' "$ASSISTANT" > "$T"
D="$R/proj/sess1/subagents"
printf '{"agentType":"general-purpose","description":"other","toolUseId":"toolu_other","spawnDepth":1}' > "$D/agent-other.meta.json"
printf '%s\n' "$DENIAL_CLASSIFIER" > "$D/agent-other.jsonl"
run_ds --desc 'turn without children' --project-root "$R"
expect "an agent of the same session that the turn did not start is not scanned" "CLEAN" 0

echo
echo "passed $PASS / $N"
[ "$FAIL" -eq 0 ]
