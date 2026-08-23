#!/usr/bin/env bash

set -u

HERE="$(cd "$(dirname "$0")" && pwd)"
REPLY="$HERE/pi_reply.js"
FIX="$HERE/fixtures"

[ -f "$REPLY" ] || { echo "pi_reply.js not found next to this script" >&2; exit 2; }
command -v node >/dev/null 2>&1 || { echo "node not on PATH (it is the delegation launcher too)" >&2; exit 2; }

PASS=0
FAIL=0
N=0

OUT=""
ERR=""
RC=0

run_reply() {
  local errfile
  errfile="$(mktemp)"
  OUT="$(node "$REPLY" "$@" 2>"$errfile")"
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
    printf '        rc=%s\n        stdout=%s\n        stderr=%s\n' "$RC" "${OUT:0:200}" "${ERR:0:200}"
  fi
}

has() { case "$2" in *"$1"*) echo 1 ;; *) echo 0 ;; esac; }

run_reply "$FIX/stream-normal.jsonl"
check "normal: exit 0" "$([ $RC -eq 0 ] && echo 1 || echo 0)"
check "normal: stderr empty" "$([ -z "$ERR" ] && echo 1 || echo 0)"
check "normal: reply text present" "$(has "needs-decision" "$OUT")"
check "normal: thinking block dropped" "$([ "$(has "thinking" "$OUT")" = "0" ] && echo 1 || echo 0)"
check "normal: no raw event envelope leaked" "$([ "$(has '"type":"turn_end"' "$OUT")" = "0" ] && echo 1 || echo 0)"
check "normal: earlier turn_end text not included" "$([ "$(has "I'll start by reading" "$OUT")" = "0" ] && echo 1 || echo 0)"

run_reply "$FIX/stream-no-turn-end.jsonl"
check "no turn_end: exit 3" "$([ $RC -eq 3 ] && echo 1 || echo 0)"
check "no turn_end: stdout empty" "$([ -z "$OUT" ] && echo 1 || echo 0)"
check "no turn_end: stderr names the reason" "$(has "no turn_end" "$ERR")"

run_reply "$FIX/stream-aborted.jsonl"
check "aborted: exit 4" "$([ $RC -eq 4 ] && echo 1 || echo 0)"
check "aborted: stdout empty" "$([ -z "$OUT" ] && echo 1 || echo 0)"
check "aborted: stderr carries stopReason" "$(has "stopReason=aborted" "$ERR")"
check "aborted: stderr carries errorMessage" "$(has "interrupted" "$ERR")"

run_reply "$FIX/stream-truncated.jsonl"
check "truncated: exit 0" "$([ $RC -eq 0 ] && echo 1 || echo 0)"
check "truncated: reply still extracted" "$(has "needs-decision" "$OUT")"

run_reply "$FIX/stream-multitext.jsonl"
check "multitext: exit 0" "$([ $RC -eq 0 ] && echo 1 || echo 0)"
check "multitext: blocks concatenated in order" \
  "$([ "$OUT" = "FIRST-BLOCK
status: ok; file: -" ] && echo 1 || echo 0)"
check "multitext: status line is last" \
  "$([ "$(printf '%s' "$OUT" | tail -n 1)" = "status: ok; file: -" ] && echo 1 || echo 0)"

run_reply
check "no argument: exit 2" "$([ $RC -eq 2 ] && echo 1 || echo 0)"
check "no argument: stdout empty" "$([ -z "$OUT" ] && echo 1 || echo 0)"

run_reply "$FIX/does-not-exist.jsonl"
check "missing file: exit 2" "$([ $RC -eq 2 ] && echo 1 || echo 0)"
check "missing file: stdout empty" "$([ -z "$OUT" ] && echo 1 || echo 0)"

echo
echo "passed $PASS / $N"
[ "$FAIL" -eq 0 ] || exit 1
