#!/usr/bin/env bash

set -u
unset CLAUDE_CODE_SESSION_ID

HERE="$(cd "$(dirname "$0")" && pwd)"
OBS="$HERE/stop_observe.js"

[ -f "$OBS" ] || { echo "stop_observe.js not found next to this script" >&2; exit 2; }
command -v node >/dev/null 2>&1 || { echo "node not on PATH (the observer runs on it)" >&2; exit 2; }
NODE="$(command -v node)"

case "$(uname -s)" in
  MINGW*|MSYS*) WINDOWS=1 ;;
  *) WINDOWS=0 ;;
esac

WORK="$(mktemp -d)"
WORK="$(cd "$WORK" && { pwd -W 2>/dev/null || pwd; })"
SPAWNED=""

cleanup() {
  local pid
  for pid in $SPAWNED; do
    taskkill //F //T //PID "$pid" >/dev/null 2>&1
  done
  rm -rf "$WORK"
}
trap cleanup EXIT

PASS=0
FAIL=0
SKIP=0
N=0

OUT=""
ERR=""
RC=0
SESS=""

run_obs() {
  local errfile
  errfile="$(mktemp)"
  OUT="$(CLAUDE_CODE_SESSION_ID="$SESS" node "$OBS" "$@" 2>"$errfile")"
  RC=$?
  ERR="$(cat "$errfile")"
  rm -f "$errfile"
}

run_obs_bare() {
  local errfile
  errfile="$(mktemp)"
  OUT="$(PATH=/usr/bin CLAUDE_CODE_SESSION_ID= "$NODE" "$OBS" "$@" 2>"$errfile")"
  RC=$?
  ERR="$(cat "$errfile")"
  rm -f "$errfile"
}

check() {
  local name="$1" cond="$2"
  N=$((N + 1))
  if [ "$cond" = "1" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %-3s %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %-3s %s\n' "$N" "$name"
    printf '        rc=%s\n        stdout=%s\n        stderr=%s\n' "$RC" "${OUT:0:200}" "${ERR:0:300}"
  fi
}

skip() {
  N=$((N + 1))
  SKIP=$((SKIP + 1))
  printf 'skip %-3s %s\n' "$N" "$1"
}

verdict_is() {
  local name="$1" want="$2" wrc="$3"
  check "$name" "$([ "$OUT" = "$want" ] && [ "$RC" = "$wrc" ] && echo 1 || echo 0)"
}

refusal() {
  local name="$1" wrc="$2"
  check "$name" "$([ "$RC" = "$wrc" ] && [ -z "$OUT" ] && [ "${ERR%%:*}" = "stop_observe" ] && echo 1 || echo 0)"
}

has() { case "$2" in *"$1"*) echo 1 ;; *) echo 0 ;; esac; }

json_of() {
  node -e 'let v=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));for(const k of process.argv[2].split(".")){v=v==null?v:v[k]}process.stdout.write(v==null?"":typeof v==="object"?JSON.stringify(v):String(v))' "$1" "$2"
}

TS='2026-10-02T00:00:00.000Z'
TEXT_LINE="{\"type\":\"assistant\",\"timestamp\":\"$TS\",\"message\":{\"content\":[{\"type\":\"text\",\"text\":\"working\"}]}}"
DENIAL_LINE="{\"type\":\"user\",\"timestamp\":\"$TS\",\"message\":{\"content\":[{\"type\":\"tool_result\",\"is_error\":true,\"tool_use_id\":\"toolu_d1\",\"content\":\"stopped\"}]},\"toolDenialKind\":\"cancelled\"}"

call_line() {
  local input="${3-}"
  [ -n "$input" ] || input='{}'
  printf '{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"tool_use","id":"%s","name":"%s","input":%s}]}}\n' "$TS" "$1" "$2" "$input"
}

result_line() {
  printf '{"type":"user","timestamp":"%s","message":{"content":[{"type":"tool_result","tool_use_id":"%s","content":"done"}]}}\n' "$TS" "$1"
}

mk_agent() {
  local root="$1" session="$2" id="$3" desc="$4" extra="${5:-}"
  local dir="$root/proj/$session/subagents"
  mkdir -p "$dir"
  printf '{"agentType":"general-purpose","description":"%s"%s}' "$desc" "${extra:+,$extra}" > "$dir/agent-$id.meta.json"
  : > "$dir/agent-$id.jsonl"
  echo "$dir/agent-$id.jsonl"
}

cc_run() {
  local root="$1" stem="$2" desc="$3" stop="$4"
  shift 4
  run_obs cc --stem "$stem" --desc "$desc" --task-stop "$stop" --cause ended --deadline 600 --window "${WINDOW:-1}" --project-root "$root" "$@"
}

new_case() {
  CASE="$WORK/$1"
  mkdir -p "$CASE"
}

SESS=sessA

new_case q1
T="$(mk_agent "$CASE" sessA aaa 'turn quiet')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn quiet' stopped
verdict_is "quiet transcript, TaskStop stopped: confirmed" "CONFIRMED" 0
check "the verdict is explained on stderr" "$(has 'CONFIRMED' "$ERR")"
check "the stop record is written next to the stem" "$([ -f "$CASE/05-1-stop.json" ] && echo 1 || echo 0)"
check "the stop record names the facet" "$([ "$(json_of "$CASE/05-1-stop.json" facet)" = "cc" ] && echo 1 || echo 0)"
check "the stop record carries the verdict" "$([ "$(json_of "$CASE/05-1-stop.json" verdict)" = "CONFIRMED" ] && echo 1 || echo 0)"
check "the stop record carries the cause and the deadline" "$([ "$(json_of "$CASE/05-1-stop.json" cause)/$(json_of "$CASE/05-1-stop.json" deadline)" = "ended/600" ] && echo 1 || echo 0)"
check "the stop record carries what TaskStop reported" "$([ "$(json_of "$CASE/05-1-stop.json" task_stop)" = "stopped" ] && echo 1 || echo 0)"
check "the stop record lists the transcript it watched" "$(has 'agent-aaa.jsonl' "$(json_of "$CASE/05-1-stop.json" transcripts)")"
check "the stop record keeps the description it resolved" "$([ "$(json_of "$CASE/05-1-stop.json" desc)" = "turn quiet" ] && echo 1 || echo 0)"
check "the stop record says what it does not observe" "$([ -n "$(json_of "$CASE/05-1-stop.json" not_observed)" ] && echo 1 || echo 0)"

new_case q2
T="$(mk_agent "$CASE" sessA aaa 'turn done before the stop')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn done before the stop' completed
verdict_is "TaskStop completed (nothing left to stop): confirmed" "CONFIRMED" 0

new_case q3
T="$(mk_agent "$CASE" sessA aaa 'turn stop failed')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn stop failed' failed
verdict_is "TaskStop failed: unconfirmed" "UNCONFIRMED" 1
check "the reason names what TaskStop reported" "$(has 'TaskStop reported failed' "$ERR")"

new_case q4
T="$(mk_agent "$CASE" sessA aaa 'turn answered call')"
{ printf '%s\n' "$TEXT_LINE"; call_line toolu_b1 Bash '{"command":"ls"}'; result_line toolu_b1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn answered call' stopped
verdict_is "a tool call that has its result does not keep the stop open" "CONFIRMED" 0

new_case q5
T="$(mk_agent "$CASE" sessA aaa 'turn open call')"
{ printf '%s\n' "$TEXT_LINE"; call_line toolu_b1 Bash '{"command":"sleep 600"}'; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn open call' stopped
verdict_is "a tool call without a result: unconfirmed" "UNCONFIRMED" 1
check "the reason names the call" "$(has 'toolu_b1 has no result' "$ERR")"

new_case q6
T="$(mk_agent "$CASE" sessA aaa 'turn background agent')"
{ call_line toolu_a1 Agent '{"prompt":"x"}'; result_line toolu_a1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn background agent' stopped
verdict_is "an Agent call is background work unless it says otherwise: unconfirmed" "UNCONFIRMED" 1
check "the reason says background work" "$(has 'background' "$ERR")"

new_case q7
T="$(mk_agent "$CASE" sessA aaa 'turn foreground agent')"
{ call_line toolu_a1 Agent '{"prompt":"x","run_in_background":false}'; result_line toolu_a1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn foreground agent' stopped
verdict_is "an Agent call that ran in the foreground and answered: confirmed" "CONFIRMED" 0

new_case q8
T="$(mk_agent "$CASE" sessA aaa 'turn background bash')"
{ call_line toolu_b1 Bash '{"command":"make","run_in_background":true}'; result_line toolu_b1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn background bash' stopped
verdict_is "a Bash call started in the background: unconfirmed" "UNCONFIRMED" 1

new_case q9
T="$(mk_agent "$CASE" sessA aaa 'turn monitor')"
{ call_line toolu_m1 Monitor '{"command":"tail -f x"}'; result_line toolu_m1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn monitor' stopped
verdict_is "a Monitor call: unconfirmed" "UNCONFIRMED" 1

new_case q10
T="$(mk_agent "$CASE" sessA aaa 'turn skill')"
{ call_line toolu_s1 Skill '{"skill":"x"}'; result_line toolu_s1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn skill' stopped
verdict_is "a Skill call, which may fork an agent: unconfirmed" "UNCONFIRMED" 1

new_case q11
T="$(mk_agent "$CASE" sessA aaa 'turn interrupted call')"
{ printf '%s\n' "$TEXT_LINE"; call_line toolu_d1 Bash '{"command":"x"}'; printf '%s\n' "$DENIAL_LINE"; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn interrupted call' stopped
verdict_is "a call refused or interrupted (the mark TaskStop leaves on a running call): unconfirmed" "UNCONFIRMED" 1
check "the reason names the denial kind" "$(has 'cancelled' "$ERR")"

new_case q12
T="$(mk_agent "$CASE" sessA aaa 'turn still writing')"
printf '%s\n' "$TEXT_LINE" > "$T"
( sleep 0.3; printf '%s\n' "$TEXT_LINE" >> "$T" ) &
WINDOW=2 cc_run "$CASE" "$CASE/05-1" 'turn still writing' stopped
wait
verdict_is "a transcript that grows during the window: unconfirmed" "UNCONFIRMED" 1
check "the reason says a transcript changed" "$(has 'changed during the observation window' "$ERR")"

new_case q13
T="$(mk_agent "$CASE" sessA aaa 'turn with a child')"
call_line toolu_c1 Agent '{"prompt":"x","run_in_background":false}' > "$T"
result_line toolu_c1 >> "$T"
K="$(mk_agent "$CASE" sessA kid1 'child' '"toolUseId":"toolu_c1"')"
call_line toolu_k1 Bash '{"command":"sleep 600"}' > "$K"
cc_run "$CASE" "$CASE/05-1" 'turn with a child' stopped
verdict_is "an open call inside an agent the turn started: unconfirmed" "UNCONFIRMED" 1

new_case q14
T="$(mk_agent "$CASE" sessA aaa 'turn with a skill child')"
printf '%s\n' "$TEXT_LINE" > "$T"
K="$(mk_agent "$CASE" sessA kid2 'forked' '"parentAgentId":"aaa"')"
printf '%s\n' "$TEXT_LINE" > "$K"
( sleep 0.3; printf '%s\n' "$TEXT_LINE" >> "$K" ) &
WINDOW=2 cc_run "$CASE" "$CASE/05-1" 'turn with a skill child' stopped
wait
verdict_is "a child that names the turn as its parent agent and still grows: unconfirmed" "UNCONFIRMED" 1

new_case q15
T="$(mk_agent "$CASE" sessA aaa 'turn with a grandchild')"
{ call_line toolu_c1 Agent '{"run_in_background":false}'; result_line toolu_c1; } > "$T"
K="$(mk_agent "$CASE" sessA kid3 'child' '"toolUseId":"toolu_c1"')"
{ call_line toolu_g1 Agent '{"run_in_background":false}'; result_line toolu_g1; } > "$K"
G="$(mk_agent "$CASE" sessA grand3 'grandchild' '"toolUseId":"toolu_g1"')"
call_line toolu_x1 Bash '{"command":"sleep 600"}' > "$G"
cc_run "$CASE" "$CASE/05-1" 'turn with a grandchild' stopped
verdict_is "an open call two agent levels below the turn: unconfirmed" "UNCONFIRMED" 1

new_case q16
T="$(mk_agent "$CASE" sessA aaa 'turn without children')"
printf '%s\n' "$TEXT_LINE" > "$T"
O="$(mk_agent "$CASE" sessA other 'unrelated' '"toolUseId":"toolu_other"')"
call_line toolu_o1 Bash '{"command":"sleep 600"}' > "$O"
cc_run "$CASE" "$CASE/05-1" 'turn without children' stopped
verdict_is "an agent of the same session the turn did not start is not watched" "CONFIRMED" 0

new_case q17
T="$(mk_agent "$CASE" sessA aaa 'turn parent of a late child')"
call_line toolu_c9 Agent '{"run_in_background":false}' > "$T"
result_line toolu_c9 >> "$T"
( sleep 0.3
  printf '{"agentType":"general-purpose","description":"late","toolUseId":"toolu_c9"}' > "$CASE/proj/sessA/subagents/agent-late.meta.json"
  printf '%s\n' "$TEXT_LINE" > "$CASE/proj/sessA/subagents/agent-late.jsonl" ) &
WINDOW=2 cc_run "$CASE" "$CASE/05-1" 'turn parent of a late child' stopped
wait
verdict_is "an agent that appears during the window: unconfirmed" "UNCONFIRMED" 1

new_case q18
mkdir -p "$CASE/proj"
cc_run "$CASE" "$CASE/05-1" 'no such turn' stopped
verdict_is "a description that resolves no transcript: unconfirmed" "UNCONFIRMED" 1
check "the reason says no transcript was found" "$(has 'no transcript' "$ERR")"

new_case q19
T="$(mk_agent "$CASE" sessA aaa 'turn without a session')"
printf '%s\n' "$TEXT_LINE" > "$T"
SESS=""
cc_run "$CASE" "$CASE/05-1" 'turn without a session' stopped
SESS=sessA
verdict_is "no session id: nothing can be resolved, unconfirmed" "UNCONFIRMED" 1

new_case q20
T="$(mk_agent "$CASE" sessB aaa 'turn of another session')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn of another session' stopped
verdict_is "a description found only in another session is not this turn" "UNCONFIRMED" 1

new_case q21
T="$(mk_agent "$CASE" sessA aaa 'turn with noise')"
{ printf '%s\n' "$TEXT_LINE"; printf 'not json at all\n'; printf '{"type":"assistant","message":{"content":[{"type":"text","text":"a\0b"}]}}\n'; call_line toolu_b1 Bash '{"command":"ls"}'; result_line toolu_b1; } > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn with noise' stopped
verdict_is "unparseable lines and a NUL byte do not hide or invent a call" "CONFIRMED" 0

new_case q22
T="$(mk_agent "$CASE" sessA aaa 'turn observed once')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn observed once' failed
cc_run "$CASE" "$CASE/05-1" 'turn observed once' stopped
verdict_is "a second look cannot turn an unconfirmed stop into a confirmed one" "UNCONFIRMED" 1
check "the second look says the first verdict stands" "$(has 'already observed' "$ERR")"

new_case q23
T="$(mk_agent "$CASE" sessA aaa 'turn confirmed once')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn confirmed once' stopped
printf '%s\n' "$TEXT_LINE" >> "$T"
cc_run "$CASE" "$CASE/05-1" 'turn confirmed once' stopped
verdict_is "a stop that was confirmed stays as recorded" "CONFIRMED" 0

new_case q24
T="$(mk_agent "$CASE" sessA aaa 'turn second attempt')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/05-1" 'turn second attempt' failed
cc_run "$CASE" "$CASE/05-2" 'turn second attempt' stopped
verdict_is "another attempt (another stem) is observed on its own" "CONFIRMED" 0

new_case q25
T="$(mk_agent "$CASE" sessA aaa 'turn bad window')"
printf '%s\n' "$TEXT_LINE" > "$T"
run_obs cc --stem "$CASE/05-1" --desc 'turn bad window' --task-stop stopped --cause ended --deadline 600 --window 61 --project-root "$CASE"
refusal "a window over the 60 s ruling is refused" 64
run_obs cc --stem "$CASE/05-1" --desc 'turn bad window' --task-stop stopped --cause ended --deadline 600 --window 0 --project-root "$CASE"
refusal "a window of 0 is refused" 64
check "a refused window writes no record" "$([ ! -e "$CASE/05-1-stop.json" ] && echo 1 || echo 0)"

new_case q26
run_obs
refusal "no arguments is a usage error" 64
run_obs cc --stem "$CASE/05-1" --desc x --task-stop stopped --cause ended --window 1
refusal "a missing deadline is a usage error" 64
run_obs cc --stem "$CASE/05-1" --desc x --task-stop stopped --cause hurried --deadline 600 --window 1
refusal "a cause other than deadline or ended is a usage error" 64
run_obs cc --stem "$CASE/05-1" --desc x --task-stop maybe --cause ended --deadline 600 --window 1
refusal "a TaskStop result other than stopped, completed or failed is a usage error" 64
run_obs cc --stem "$CASE/05-1" --task-stop stopped --cause ended --deadline 600 --window 1
refusal "cc without a description is a usage error" 64
run_obs cc --stem "$CASE/05-1" --desc x --task-stop stopped --cause ended --deadline 0 --window 1
refusal "a deadline of 0 is a usage error" 64
run_obs cc --stem "$CASE/05-1" --desc x --task-stop stopped --cause ended --deadline 600 --window 1 --bogus 1
refusal "an unknown flag is a usage error" 64
run_obs somewhere-else --stem "$CASE/05-1"
refusal "an unknown subcommand is a usage error" 64
run_obs cc --stem
refusal "a flag given last with no value is a usage error, not a hang" 64

new_case q29
printf '{"verdict":"MAYBE","reasons":[]}' > "$CASE/05-1-stop.json"
cc_run "$CASE" "$CASE/05-1" 'turn odd record' stopped
verdict_is "a stop record that does not say exactly CONFIRMED reads as unconfirmed" "UNCONFIRMED" 1

new_case q30
T="$(mk_agent "$CASE" sessA aaa 'turn starts a call late')"
printf '%s
' "$TEXT_LINE" > "$T"
( sleep 0.3; call_line toolu_late Bash '{"command":"sleep 600"}' >> "$T" ) &
WINDOW=2 cc_run "$CASE" "$CASE/05-1" 'turn starts a call late' stopped
wait
verdict_is "a call started during the window: unconfirmed" "UNCONFIRMED" 1
check "the reason names the call open at the end of the window" "$(has 'at the end of the window' "$ERR")"

new_case q28
T="$(mk_agent "$CASE" sessA aaa 'turn meta only')"
rm -f "$T"
cc_run "$CASE" "$CASE/05-1" 'turn meta only' stopped
verdict_is "an agent whose transcript file does not exist: unconfirmed" "UNCONFIRMED" 1

new_case q27
T="$(mk_agent "$CASE" sessA aaa 'turn no run dir')"
printf '%s\n' "$TEXT_LINE" > "$T"
cc_run "$CASE" "$CASE/no-such-dir/05-1" 'turn no run dir' stopped
refusal "a stem in a missing directory is refused, not guessed" 1

if [ "$WINDOWS" = 1 ]; then
  start_sleeper() {
    node -e 'setTimeout(()=>{},120000)' < /dev/null > /dev/null 2>&1 &
    local msys=$! tries=0 win=""
    while [ "$tries" -lt 50 ]; do
      win="$(cat /proc/$msys/winpid 2>/dev/null)"
      [ -n "$win" ] && break
      sleep 0.1
      tries=$((tries + 1))
    done
    SPAWNED="$SPAWNED $win"
    SLEEPER="$win"
  }

  new_case w1
  start_sleeper
  P1="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$P1"
  verdict_is "pids records a running process silently" "" 0
  check "the record holds the PID, the image and the creation time" "$([ "$(json_of "$CASE/05-1-pids.json" processes.0.pid)" = "$P1" ] && [ "$(json_of "$CASE/05-1-pids.json" processes.0.image)" = "node.exe" ] && [[ "$(json_of "$CASE/05-1-pids.json" processes.0.created)" =~ ^[0-9]{17,19}$ ]] && echo 1 || echo 0)"

  run_obs pids --stem "$CASE/05-1" "$P1"
  refusal "processes are recorded once, at launch" 1
  check "the refusal says they were already recorded" "$(has 'already' "$ERR")"

  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 2
  verdict_is "a recorded process that still runs: unconfirmed" "UNCONFIRMED" 1
  check "the reason names it" "$(has "still running at the end of the window: $P1" "$ERR")"
  check "the record marks the process as running" "$([ "$(json_of "$CASE/05-1-stop.json" processes.0.running)" = "true" ] && echo 1 || echo 0)"
  check "the record says what it does not observe" "$(has 'outside the recorded set' "$(json_of "$CASE/05-1-stop.json" not_observed)")"

  run_obs pids --stem "$CASE/05-1" "$P1"
  refusal "no processes are recorded after the stop was observed" 1

  new_case w2
  start_sleeper
  P2="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$P2"
  taskkill //F //PID "$P2" >/dev/null 2>&1
  run_obs pi-cli --stem "$CASE/05-1" --cause deadline --deadline 600 --window 5
  verdict_is "the recorded process is gone: confirmed" "CONFIRMED" 0
  check "the record keeps the cause and the deadline" "$([ "$(json_of "$CASE/05-1-stop.json" cause)/$(json_of "$CASE/05-1-stop.json" deadline)" = "deadline/600" ] && echo 1 || echo 0)"

  new_case w3
  start_sleeper
  P3="$SLEEPER"
  taskkill //F //PID "$P3" >/dev/null 2>&1
  sleep 1
  run_obs pids --stem "$CASE/05-1" "$P3"
  refusal "a PID that is not running is not recorded" 1
  check "nothing is written for it" "$([ ! -e "$CASE/05-1-pids.json" ] && echo 1 || echo 0)"

  new_case w4
  run_obs pids --stem "$CASE/05-1" 4
  refusal "a process whose image or creation time cannot be read is not recorded" 1

  new_case w5
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 1
  verdict_is "no process set recorded at launch: unconfirmed" "UNCONFIRMED" 1
  check "the reason says none was recorded" "$(has 'no process set was recorded' "$ERR")"

  new_case w14
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 1
  start_sleeper
  P14="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$P14"
  refusal "a stop that was observed with nothing recorded closes the launch: nothing is recorded afterwards" 1
  check "no record appeared after the stop" "$([ ! -e "$CASE/05-1-pids.json" ] && echo 1 || echo 0)"

  new_case w6
  start_sleeper
  A="$SLEEPER"
  start_sleeper
  B="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$A" "$B"
  taskkill //F //PID "$A" >/dev/null 2>&1
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 2
  verdict_is "one of two recorded processes still runs: unconfirmed" "UNCONFIRMED" 1
  check "the reason names the one that runs" "$(has "$B" "$ERR")"

  new_case w7
  start_sleeper
  A="$SLEEPER"
  start_sleeper
  B="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$A" "$B"
  taskkill //F //PID "$A" //PID "$B" >/dev/null 2>&1
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 5
  verdict_is "both recorded processes are gone: confirmed" "CONFIRMED" 0

  new_case w8
  start_sleeper
  R1="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$R1"
  node -e 'const f=process.argv[1];const r=JSON.parse(require("fs").readFileSync(f,"utf8"));r.processes[0].created="1";require("fs").writeFileSync(f,JSON.stringify(r))' "$CASE/05-1-pids.json"
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 5
  verdict_is "a live process that carries the PID but was created at another time is not the recorded one" "CONFIRMED" 0

  new_case w9
  printf '{"processes":[{"pid":4,"image":"System","created":"1"}]}' > "$CASE/05-1-pids.json"
  run_obs pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 1
  verdict_is "a recorded process that cannot be read now still counts as running" "UNCONFIRMED" 1

  new_case w10
  start_sleeper
  P10="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$P10"
  taskkill //F //PID "$P10" >/dev/null 2>&1
  run_obs_bare pi-cli --stem "$CASE/05-1" --cause ended --deadline 600 --window 1
  verdict_is "with no way to identify processes nothing is confirmed" "UNCONFIRMED" 1

  new_case w11
  start_sleeper
  P11="$SLEEPER"
  run_obs_bare pids --stem "$CASE/05-1" "$P11"
  refusal "with no way to identify processes none is recorded" 1

  new_case w13
  start_sleeper
  D1="$SLEEPER"
  run_obs pids --stem "$CASE/05-1" "$D1" "$D1"
  check "the same PID given twice is recorded once" "$([ "$(json_of "$CASE/05-1-pids.json" processes.0.pid)" = "$D1" ] && [ -z "$(json_of "$CASE/05-1-pids.json" processes.1.pid)" ] && echo 1 || echo 0)"

  new_case w12
  run_obs pids --stem "$CASE/05-1" abc
  refusal "a PID that is not a number is a usage error" 64
  run_obs pids --stem "$CASE/05-1" 0
  refusal "a PID of 0 is a usage error" 64
  run_obs pids --stem "$CASE/05-1"
  refusal "pids without a PID is a usage error" 64
  run_obs pids --stem "$CASE/05-1" ""
  refusal "an empty PID, as a failed translation leaves it, is a usage error" 64
  check "the failed recordings left no file" "$([ ! -e "$CASE/05-1-pids.json" ] && echo 1 || echo 0)"

  GITBASH="$(cygpath -u "${PROGRAMFILES:-C:/Program Files}")/Git/bin/bash.exe"
  if [ -x "$GITBASH" ]; then
    launch_through_wrapper() {
      new_case "$1"
      WRAP_STEM="$CASE/05-1"
      export OBS WRAP_STEM
      "$GITBASH" -c 'node -e "setTimeout(()=>{},120000)" < /dev/null &
child=$!
node "$OBS" pids --stem "$WRAP_STEM" "$(cat /proc/$child/winpid)" "$(cat /proc/$$/winpid)" || echo "processes not recorded" >&2
wait $child' > /dev/null 2>&1 &
      WRAP_MSYS=$!
      local tries=0
      while [ "$tries" -lt 100 ] && [ ! -f "$WRAP_STEM-pids.json" ]; do
        sleep 0.1
        tries=$((tries + 1))
      done
      WRAP_WIN="$(cat /proc/$WRAP_MSYS/winpid 2>/dev/null)"
      SPAWNED="$SPAWNED $WRAP_WIN $(json_of "$WRAP_STEM-pids.json" processes.0.pid) $(json_of "$WRAP_STEM-pids.json" processes.1.pid)"
    }

    launch_through_wrapper x1
    check "a launch line run by Git's bash records the CLI and the shell" "$([ "$(json_of "$WRAP_STEM-pids.json" processes.0.image)" = "node.exe" ] && [ -n "$(json_of "$WRAP_STEM-pids.json" processes.1.pid)" ] && echo 1 || echo 0)"
    taskkill //F //T //PID "$WRAP_WIN" >/dev/null 2>&1
    run_obs pi-cli --stem "$WRAP_STEM" --cause deadline --deadline 600 --window 10
    verdict_is "the tree kill of the wrapper takes the recorded processes with it: confirmed" "CONFIRMED" 0

    launch_through_wrapper x2
    taskkill //F //PID "$WRAP_WIN" >/dev/null 2>&1
    run_obs pi-cli --stem "$WRAP_STEM" --cause deadline --deadline 600 --window 2
    verdict_is "killing only the root leaves the recorded CLI running: unconfirmed" "UNCONFIRMED" 1
  else
    skip "Git's bash wrapper is not installed here"
  fi
else
  skip "process identity is read on Windows only: no pi-cli case runs here"
fi

echo
echo "passed $PASS / $((N - SKIP)) (skipped $SKIP)"
[ "$FAIL" -eq 0 ]
