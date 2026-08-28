#!/usr/bin/env bash

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
WATCHDOG="$HERE/watchdog.sh"

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

newcase args
run_wd --desc "d" --deadline 2
expect "--deliv is required" "" 64
run_wd --deliv "$DELIV" --deadline 2
expect "--desc is required" "" 64
run_wd --deliv "$DELIV" --desc "d" --totally-bogus-flag
expect "an unknown argument is rejected" "" 64

newcase deadline-override
run_wd --deliv - --desc "d" --mode plan --deadline 2 --poll 1 --project-root "$PROOT"
expect "--deadline overrides the mode's budget" TIMEOUT 1

expect_grep "STALL is configured at the top of the script" '^STALL=600$' "$WATCHDOG"
expect_grep "DEADLINE_SURVEY is 3600" '^DEADLINE_SURVEY=3600$' "$WATCHDOG"
expect_grep "DEADLINE_PLAN is 2400" '^DEADLINE_PLAN=2400$' "$WATCHDOG"
expect_grep "DEADLINE_EXECUTE is 1500" '^DEADLINE_EXECUTE=1500$' "$WATCHDOG"
expect_grep "DEADLINE_DEBUG is 900" '^DEADLINE_DEBUG=900$' "$WATCHDOG"
expect_grep "DEADLINE_REVIEW is 900" '^DEADLINE_REVIEW=900$' "$WATCHDOG"
expect_grep "DEADLINE_REVIEW_DEV is 1800" '^DEADLINE_REVIEW_DEV=1800$' "$WATCHDOG"
expect_grep "DEADLINE_DEFAULT is 900" '^DEADLINE_DEFAULT=900$' "$WATCHDOG"

echo
printf '%d passed, %d failed (%d total)\n' "$PASS" "$FAIL" "$N"
[ "$FAIL" -eq 0 ] || exit 1
