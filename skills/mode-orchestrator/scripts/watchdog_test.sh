#!/usr/bin/env bash

set -u
unset CLAUDE_CODE_SESSION_ID

HERE="$(cd "$(dirname "$0")" && pwd)"
WATCHDOG="$HERE/watchdog.sh"
HARNESS_CC="$HERE/../references/harness-cc.md"

[ -f "$WATCHDOG" ] || { echo "watchdog.sh not found next to this script" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
N=0

WD_OUT=""
WD_RC=0
WD_ERR=""
WD_STDOUT=""
WD_PID=""

run_wd() {
  WD_ERR="${CASE:-$WORK}/wd.err"
  WD_OUT="$(bash "$WATCHDOG" "$@" 2>"$WD_ERR")"
  WD_RC=$?
}

bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout 10 "$@"
  else
    "$@"
  fi
}

start_wd() {
  WD_ERR="$CASE/wd.err"
  WD_STDOUT="$CASE/wd.out"
  bash "$WATCHDOG" "$@" >"$WD_STDOUT" 2>"$WD_ERR" &
  WD_PID=$!
}

wait_wd() {
  wait "$WD_PID"
  WD_RC=$?
  WD_OUT="$(cat "$WD_STDOUT")"
}

expect() {
  local name="$1" ew="$2" erc="$3"
  N=$((N + 1))
  if [ "$WD_OUT" = "$ew" ] && [ "$WD_RC" = "$erc" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %2d  %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %2d  %s — expected [%s]/%s, got [%s]/%s\n' \
      "$N" "$name" "$ew" "$erc" "$WD_OUT" "$WD_RC"
  fi
}

expect_alive() {
  local name="$1"
  N=$((N + 1))
  if kill -0 "$WD_PID" 2>/dev/null; then
    PASS=$((PASS + 1))
    printf 'ok   %2d  %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %2d  %s — watchdog already exited\n' "$N" "$name"
  fi
}

expect_grep() {
  local name="$1" pat="$2" file="$3"
  N=$((N + 1))
  if grep -qE "$pat" "$file" 2>/dev/null; then
    PASS=$((PASS + 1))
    printf 'ok   %2d  %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %2d  %s — pattern not found: %s\n' "$N" "$name" "$pat"
  fi
}

expect_empty_dir() {
  local name="$1" dir="$2" left
  N=$((N + 1))
  left="$(ls -A "$dir" 2>/dev/null)"
  if [ -z "$left" ]; then
    PASS=$((PASS + 1))
    printf 'ok   %2d  %s\n' "$N" "$name"
  else
    FAIL=$((FAIL + 1))
    printf 'FAIL %2d  %s — left behind: %s\n' "$N" "$name" "$(echo $left)"
  fi
}

now() { date +%s; }

newcase() {
  CASE="$WORK/$1"
  mkdir -p "$CASE"
  DELIV="$CASE/NN-mode.md"
  PROOT="$CASE/projects"
  mkdir -p "$PROOT"
}

fake_agent() {
  local desc="$1" meta_age="$2" jsonl_age="$3"
  local dir="$PROOT/some-project/session-id/subagents"
  mkdir -p "$dir"
  printf '{"description":"%s"}\n' "$desc" > "$dir/agent-x.meta.json"
  printf 'line\n' > "$dir/agent-x.jsonl"
  touch -d "@$(( $(now) - meta_age ))" "$dir/agent-x.meta.json"
  touch -d "@$(( $(now) - jsonl_age ))" "$dir/agent-x.jsonl"
  AGENT_JSONL="$dir/agent-x.jsonl"
}

echo "watchdog self-tests"
echo

newcase deliv-fresh
start_wd --deliv "$DELIV" --desc "d1" --deadline 12 --poll 1 \
  --project-root "$PROOT" --log "$CASE/trace.log"
sleep 2
printf 'content\n' > "$DELIV"
sleep 4
expect_alive "the watchdog keeps running after the deliverable appears"
wait_wd
expect "a turn whose deliverable was written still ends at its deadline" TIMEOUT 1
expect_grep "the deliverable observation is traced, not exited on" \
  "deliverable written after [0-9]+s — continuing to monitor" "$CASE/trace.log"
expect_grep "TIMEOUT says the deliverable was written" \
  "TIMEOUT — .*deliverable written [0-9]+s in" "$WD_ERR"

newcase deliv-stale
printf 'left over from a previous attempt\n' > "$DELIV"
touch -d "@$(( $(now) - 300 ))" "$DELIV"
run_wd --deliv "$DELIV" --desc "d2" --deadline 3 --poll 1 --project-root "$PROOT"
expect "a deliverable predating t0 does not count as this turn's" TIMEOUT 1
expect_grep "TIMEOUT says no deliverable was written" \
  "TIMEOUT — .*no deliverable written this turn" "$WD_ERR"

newcase deliv-stale-then-fresh
printf 'left over\n' > "$DELIV"
touch -d "@$(( $(now) - 300 ))" "$DELIV"
start_wd --deliv "$DELIV" --desc "d3" --deadline 12 --poll 1 \
  --project-root "$PROOT" --log "$CASE/trace.log"
sleep 2
printf 'this attempt\n' > "$DELIV"
sleep 4
expect_alive "an overwritten stale deliverable does not end the run either"
expect_grep "the overwrite is recognised as this turn's write" \
  "deliverable written after [0-9]+s — continuing to monitor" "$CASE/trace.log"
wait_wd
expect "the re-run case still ends at its deadline" TIMEOUT 1

newcase deliv-empty
: > "$DELIV"
run_wd --deliv "$DELIV" --desc "d4" --deadline 3 --poll 1 --project-root "$PROOT"
expect "a zero-byte deliverable does not count as written" TIMEOUT 1
expect_grep "a zero-byte deliverable is reported as not written" \
  "TIMEOUT — .*no deliverable written this turn" "$WD_ERR"

newcase deliv-none
run_wd --deliv - --desc "d5" --deadline 3 --poll 1 --project-root "$PROOT"
expect "--deliv - bounds on deadline alone" TIMEOUT 1

newcase deliv-stale-trace
printf 'left over\n' > "$DELIV"
touch -d "@$(( $(now) - 300 ))" "$DELIV"
run_wd --deliv "$DELIV" --desc "d6" --deadline 3 --poll 1 \
  --project-root "$PROOT" --log "$CASE/trace.log"
expect_grep "a stale deliverable is called out in the trace" "stale, ignoring" "$CASE/trace.log"

newcase stall-idle
fake_agent "turn desc stall" 5 120
run_wd --deliv "$DELIV" --desc "turn desc stall" --deadline 30 --stall 3 --poll 1 \
  --project-root "$PROOT"
expect "an idle transcript is STALL" STALL 2

newcase stall-active
fake_agent "turn desc active" 5 0
( for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do printf 'line\n' >> "$AGENT_JSONL"; sleep 0.5; done ) &
APPENDER=$!
run_wd --deliv "$DELIV" --desc "turn desc active" --deadline 4 --stall 3 --poll 1 \
  --project-root "$PROOT"
expect "a transcript still being appended does not STALL" TIMEOUT 1
kill "$APPENDER" 2>/dev/null
wait "$APPENDER" 2>/dev/null

newcase stall-after-deliv
fake_agent "turn desc tail hang" 5 0
start_wd --deliv "$DELIV" --desc "turn desc tail hang" --deadline 60 --stall 6 --poll 1 \
  --project-root "$PROOT"
sleep 1
printf 'content\n' > "$DELIV"
wait_wd
expect "a hung tail after the deliverable is STALL, not an early exit" STALL 2
expect_grep "STALL says the deliverable was already written" \
  "STALL — .*deliverable written [0-9]+s in" "$WD_ERR"

newcase resolve-prefix
fake_agent "Turn02 plan extra" 5 120
run_wd --deliv "$DELIV" --desc "Turn02 plan" --deadline 3 --stall 2 --poll 1 \
  --project-root "$PROOT"
expect "a description that is only a prefix does not match" TIMEOUT 1

newcase resolve-backdate
fake_agent "turn desc old" 600 120
run_wd --deliv "$DELIV" --desc "turn desc old" --deadline 3 --stall 2 --poll 1 \
  --project-root "$PROOT"
expect "an agent record older than the backdate window is ignored" TIMEOUT 1

newcase resolve-absent
run_wd --deliv "$DELIV" --desc "no such turn" --deadline 3 --stall 2 --poll 1 \
  --project-root "$PROOT"
expect "an unresolvable transcript degrades to deadline only" TIMEOUT 1

newcase resolve-other-session
fake_agent "turn desc elsewhere" 5 120
export CLAUDE_CODE_SESSION_ID=another-session
run_wd --deliv "$DELIV" --desc "turn desc elsewhere" --deadline 3 --stall 2 --poll 1 \
  --project-root "$PROOT"
unset CLAUDE_CODE_SESSION_ID
expect "an agent record in another session is not this turn's" TIMEOUT 1

newcase resolve-own-session
fake_agent "turn desc own" 5 120
export CLAUDE_CODE_SESSION_ID=session-id
run_wd --deliv "$DELIV" --desc "turn desc own" --deadline 30 --stall 3 --poll 1 \
  --project-root "$PROOT"
unset CLAUDE_CODE_SESSION_ID
expect "an agent record in the calling session is resolved" STALL 2

newcase killed-leaves-nothing
mkdir -p "$CASE/tmp"
TMPDIR="$CASE/tmp" bash "$WATCHDOG" --deliv "$DELIV" --desc "d7" --deadline 30 --poll 1 \
  --project-root "$PROOT" --log "$CASE/trace.log" >/dev/null 2>&1 &
WD_PID=$!
for _ in 1 2 3 4 5 6 7 8 9 10; do
  grep -q '] start ' "$CASE/trace.log" 2>/dev/null && break
  sleep 1
done
expect_grep "the watchdog to be killed had started" '\] start ' "$CASE/trace.log"
kill -KILL "$WD_PID" 2>/dev/null
wait "$WD_PID" 2>/dev/null
expect_empty_dir "a SIGKILLed watchdog leaves no temp files (win32 TaskStop runs no trap)" "$CASE/tmp"

newcase args
run_wd --desc "d" --deadline 2
expect "--deliv is required" "" 64
run_wd --deliv "$DELIV" --deadline 2
expect "--desc is required" "" 64
run_wd --deliv "$DELIV" --desc "d" --totally-bogus-flag
expect "an unknown argument is rejected" "" 64
for flag in --deliv --desc --mode --deadline --stall --poll --project-root --log; do
  WD_OUT="$(bounded bash "$WATCHDOG" --deliv - --desc d "$flag" 2>/dev/null)"
  WD_RC=$?
  expect "a bare $flag is a usage error, not a hang" "" 64
done

newcase deadline-override
run_wd --deliv - --desc "d" --mode plan --deadline 2 --poll 1 --project-root "$PROOT"
expect "--deadline overrides the mode's budget" TIMEOUT 1

expect_grep "STALL is configured at the top of the script" '^STALL=600$' "$WATCHDOG"
expect_grep "DEADLINE_SURVEY is 7200" '^DEADLINE_SURVEY=7200$' "$WATCHDOG"
expect_grep "DEADLINE_PLAN is 4800" '^DEADLINE_PLAN=4800$' "$WATCHDOG"
expect_grep "DEADLINE_EXECUTE is 4800" '^DEADLINE_EXECUTE=4800$' "$WATCHDOG"
expect_grep "DEADLINE_DEBUG is 1800" '^DEADLINE_DEBUG=1800$' "$WATCHDOG"
expect_grep "DEADLINE_REVIEW_PROCESS is 1800" '^DEADLINE_REVIEW_PROCESS=1800$' "$WATCHDOG"
expect_grep "DEADLINE_REVIEW_DEV is 3600" '^DEADLINE_REVIEW_DEV=3600$' "$WATCHDOG"
expect_grep "DEADLINE_DEFAULT is 1800" '^DEADLINE_DEFAULT=1800$' "$WATCHDOG"
expect_grep "POLL is 15" '^POLL=15$' "$WATCHDOG"
expect_grep "the P2 launch line execs, so stopping the task reaches the watchdog" '^exec bash <this skill.s dir>/scripts/watchdog\.sh ' "$HARNESS_CC"

echo
printf '%d passed, %d failed (%d total)\n' "$PASS" "$FAIL" "$N"
[ "$FAIL" -eq 0 ] || exit 1
