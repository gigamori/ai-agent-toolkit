#!/usr/bin/env bash

set -u
unset CLAUDE_CODE_SESSION_ID

HERE="$(cd "$(dirname "$0")" && pwd)"
GATE="$HERE/judge_gate.js"

[ -f "$GATE" ] || { echo "judge_gate.js not found next to this script" >&2; exit 2; }
command -v node >/dev/null 2>&1 || { echo "node not on PATH (the gate runs on it)" >&2; exit 2; }

case "$(uname -s)" in
  MINGW*|MSYS*) WINDOWS=1 ;;
  *) WINDOWS=0 ;;
esac

WORK="$(mktemp -d)" && [ -d "$WORK" ] || { echo "mktemp -d failed (no temp dir to work in)" >&2; exit 2; }
WORK="$(cd "$WORK" && { pwd -W 2>/dev/null || pwd; })"
trap 'rm -rf "$WORK"' EXIT

PASS=0
FAIL=0
SKIP=0
N=0

OUT=""
ERR=""
RC=0

run_gate() {
  local errfile
  errfile="$(mktemp)"
  OUT="$(node "$GATE" "$@" 2>"$errfile")"
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
    printf '        rc=%s\n        stdout=%s\n        stderr=%s\n' "$RC" "${OUT:0:300}" "${ERR:0:400}"
  fi
}

skip() {
  N=$((N + 1))
  SKIP=$((SKIP + 1))
  printf 'skip %-3s %s\n' "$N" "$1"
}

has() { case "$2" in *"$1"*) echo 1 ;; *) echo 0 ;; esac; }

has_all() {
  local hay="$1" needle
  shift
  [ -n "$hay" ] || { echo 0; return; }
  for needle in "$@"; do
    [ -n "$needle" ] || { echo 0; return; }
    [ "$(has "$needle" "$hay")" = "1" ] || { echo 0; return; }
  done
  echo 1
}

json_of() {
  node -e 'let v=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));for(const k of process.argv[2].split(".")){v=v==null?v:v[k]}process.stdout.write(v==null?"":typeof v==="object"?JSON.stringify(v):String(v))' "$1" "$2"
}

refused() {
  check "$1" "$([ "$RC" = "1" ] && { [ -z "$OUT" ] || [ "$OUT" = "REFUSED" ]; } && [ "${ERR%%:*}" = "judge_gate" ] && echo 1 || echo 0)"
}

allowed() {
  check "$1" "$([ "$RC" = "0" ] && [ "${OUT%% *}" = "ALLOW" ] && [ "$(printf '%s' "$OUT" | cut -d' ' -f2)" = "$2" ] && echo 1 || echo 0)"
}

usage_error() {
  check "$1" "$([ "$RC" = "64" ] && [ -z "$OUT" ] && [ "${ERR%%:*}" = "judge_gate" ] && echo 1 || echo 0)"
}

T0='2026-10-02T00:00:00.000Z'
T1='2026-10-02T00:01:00.000Z'
T2='2026-10-02T00:11:40.000Z'

mk_transcript() {
  {
    printf '{"type":"user","timestamp":"%s","message":{"content":"task"}}\n' "$T0"
    printf '{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"text","text":"writing the file whole"}]}}\n' "$T1"
    printf '{"type":"assistant","timestamp":"%s","message":{"content":[{"type":"text","text":"a late line"}]}}\n' "$T2"
  } > "$1"
}

mk_stop() {
  printf '{"facet":"%s","cause":"%s","deadline":600,"verdict":"%s","reasons":[],"transcripts":%s}\n' "$2" "$3" "$4" "$5" > "$1-stop.json"
}

new_attempt() {
  CASE="$WORK/$1"
  mkdir -p "$CASE"
  STEM="$CASE/05-1"
  TRANS="$CASE/agent-aaa.jsonl"
  DELIV="$CASE/05-execute.md"
  RECORD="$STEM-judge.md"
  mk_transcript "$TRANS"
  printf 'change report\nsecond line\n' > "$DELIV"
  mk_stop "$STEM" cc "${2:-ended}" "${3:-CONFIRMED}" "[\"$TRANS\"]"
}

issue_case() {
  new_attempt "$@"
  run_gate issue --stem "$STEM" --deliv "$DELIV"
  SENTINEL="$(json_of "$STEM-evidence.json" judge.sentinel)"
}

mk_record() {
  cat > "$RECORD"
  printf '%s\n' "$SENTINEL" >> "$RECORD"
}

allow_record() {
  mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
}

deadline_record() {
  mk_record <<EOF
verdict: allow
method: restart
next-timeout: ${2:-900}
evidence:
- $TRANS#${1:-L2-L2}: the attempt was still writing
limits: none
EOF
}

ask() {
  local reply="allow $RECORD"
  [ $# -ge 1 ] && reply="$1"
  run_gate check --stem "$STEM" --reply "$reply"
}

issue_case i1
check "issue prints the delegation facts on one line" "$([ "$RC" = "0" ] && [ "$(printf '%s\n' "$OUT" | wc -l)" = "1" ] && echo 1 || echo 0)"
check "the line starts with the judge's prompt path" "$([ -n "$OUT" ] && [ "$(printf '%s' "$OUT" | cut -d' ' -f1)" = "$(json_of "$STEM-evidence.json" judge.prompt_path)" ] && echo 1 || echo 0)"
check "the line names the model class, the tools, the record, the timeout and the description" "$(has_all "$OUT" '(judge, model=ultra, tools=Read,Grep,Write, record=' 'timeout=900' 'description=mo-judge ')"
check "issue writes the judge prompt" "$([ -s "$STEM-judge-prompt.md" ] && echo 1 || echo 0)"
check "issue writes the evidence manifest" "$([ -s "$STEM-evidence.json" ] && echo 1 || echo 0)"
check "issue leaves the record to the judge" "$([ ! -e "$RECORD" ] && echo 1 || echo 0)"

issue_case i2
check "the manifest lists the deliverable, the stop record and the transcript, in that order" "$([ "$(json_of "$STEM-evidence.json" files.0.kind),$(json_of "$STEM-evidence.json" files.1.kind),$(json_of "$STEM-evidence.json" files.2.kind)" = "deliverable,stop,transcript" ] && echo 1 || echo 0)"
check "each entry carries the checksum the file has" "$([ "$(json_of "$STEM-evidence.json" files.2.sha256)" = "$(sha256sum "$TRANS" | cut -d' ' -f1)" ] && echo 1 || echo 0)"
check "each entry carries its line count" "$([ "$(json_of "$STEM-evidence.json" files.2.lines)" = "3" ] && [ "$(json_of "$STEM-evidence.json" files.0.lines)" = "2" ] && echo 1 || echo 0)"
check "the manifest carries the cause and the attempt's deadline" "$([ "$(json_of "$STEM-evidence.json" cause)/$(json_of "$STEM-evidence.json" d_prev)" = "ended/600" ] && echo 1 || echo 0)"
check "the manifest takes the start from the first stamped line of the transcript" "$([ "$(json_of "$STEM-evidence.json" t0)" = "$(date -u -d "$T0" +%s)" ] && echo 1 || echo 0)"
check "the manifest carries the judge's own end marker and deadline" "$([ "$(has '[[MO-JUDGE-END ' "$(json_of "$STEM-evidence.json" judge.sentinel)")" = "1" ] && [ "$(json_of "$STEM-evidence.json" judge.timeout)" = "900" ] && echo 1 || echo 0)"

issue_case i3
check "the prompt names the record and the listed files" "$(has_all "$(cat "$STEM-judge-prompt.md")" "$(json_of "$STEM-evidence.json" judge.record_path)" "$(json_of "$STEM-evidence.json" files.2.path)")"
check "the prompt ends its record template with the end marker" "$([ -n "$SENTINEL" ] && has "$SENTINEL" "$(cat "$STEM-judge-prompt.md")" || echo 0)"
check "an attempt that ended gets no next-timeout line" "$([ "$(has 'next-timeout' "$(cat "$STEM-judge-prompt.md")")" = "0" ] && echo 1 || echo 0)"
check "the prompt says why the attempt stopped" "$(has 'it ended without a valid result' "$(cat "$STEM-judge-prompt.md")")"

issue_case i4 deadline
check "an attempt that hit its deadline gets the next-timeout line" "$(has 'next-timeout: <seconds>' "$(cat "$STEM-judge-prompt.md")")"
check "the prompt states the deadline that was hit" "$(has 'deadline of 600 seconds' "$(cat "$STEM-judge-prompt.md")")"
check "the prompt says why the attempt stopped" "$(has 'its deadline passed' "$(cat "$STEM-judge-prompt.md")")"

new_attempt i5
rm -f "$STEM-stop.json"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "no stop record: no judge"
check "the refusal says the stop must be observed first" "$(has 'stop' "$ERR")"
check "a refusal writes nothing" "$([ ! -e "$STEM-evidence.json" ] && [ ! -e "$STEM-judge-prompt.md" ] && echo 1 || echo 0)"

new_attempt i6 ended UNCONFIRMED
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "an unconfirmed stop: no judge"
check "the refusal says it is never run again" "$(has 'not confirmed' "$ERR")"

issue_case i7
FIRST="$(sha256sum "$STEM-evidence.json" | cut -d' ' -f1)"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "a second issue for the same attempt is refused"
check "the first manifest stands" "$([ -n "$FIRST" ] && [ "$(sha256sum "$STEM-evidence.json" | cut -d' ' -f1)" = "$FIRST" ] && echo 1 || echo 0)"

new_attempt i8
mk_stop "$STEM" cc ended CONFIRMED '[]'
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "a stop record that lists no transcript: no judge"

new_attempt i9
mk_stop "$STEM" cc ended CONFIRMED "[\"$CASE/no-such.jsonl\"]"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "a listed transcript that does not exist: no judge"

new_attempt i10
rm -f "$DELIV"
run_gate issue --stem "$STEM" --deliv "$DELIV"
check "a deliverable that was never written is simply not listed" "$([ "$RC" = "0" ] && [ "$(json_of "$STEM-evidence.json" files.0.kind),$(json_of "$STEM-evidence.json" files.1.kind)" = "stop,transcript" ] && echo 1 || echo 0)"

new_attempt i11
run_gate issue --stem "$STEM" --deliv -
check "a turn that writes no file passes a dash for the deliverable" "$([ "$RC" = "0" ] && [ "$(json_of "$STEM-evidence.json" files.0.kind)" = "stop" ] && echo 1 || echo 0)"

new_attempt i12
printf '{"type":"user","message":{"content":"no stamp"}}\n' > "$TRANS"
run_gate issue --stem "$STEM" --deliv "$DELIV"
check "a transcript with no stamped line gives no start time" "$([ "$RC" = "0" ] && [ -z "$(json_of "$STEM-evidence.json" t0)" ] && echo 1 || echo 0)"

new_attempt i13
mk_stop "$STEM" somewhere ended CONFIRMED "[\"$TRANS\"]"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "a stop record of an unknown facet: no judge"

new_attempt i14
STEM="$CASE/05-2"
mk_stop "$STEM" cc ended CONFIRMED "[\"$TRANS\"]"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "a stem that is not an attempt's first run: no judge"
check "a refusal for a later attempt writes nothing" "$([ ! -e "$STEM-evidence.json" ] && [ ! -e "$STEM-judge-prompt.md" ] && echo 1 || echo 0)"

run_gate
usage_error "no arguments is a usage error"
run_gate issue
usage_error "issue without a stem is a usage error"
run_gate somewhere-else --stem "$WORK/x"
usage_error "an unknown subcommand is a usage error"
run_gate issue --stem
usage_error "a flag given last with no value is a usage error, not a hang"
run_gate check --stem "$WORK/x"
usage_error "check without a reply is a usage error"

mk_pi_attempt() {
  CASE="$WORK/$1"
  mkdir -p "$CASE/raw" "$CASE/sessions"
  STEM="$CASE/05-1"
  RAW="$CASE/raw/05-1.jsonl"
  SD="$CASE/sessions"
  SESSION_FILE="$SD/2026-10-02T00-00-00-000Z_11111111-aaaa.jsonl"
  RECORD="$STEM-judge.md"
  printf '{"type":"session","version":3,"id":"11111111-aaaa","timestamp":"%s","cwd":"x"}\n' "$T0" > "$RAW"
  {
    printf '{"type":"session","version":3,"id":"11111111-aaaa","timestamp":"%s","cwd":"x"}\n' "$T0"
    printf '{"type":"message","id":"a1","timestamp":"%s","message":{"role":"assistant","content":[{"type":"text","text":"writing"}]}}\n' "$T1"
  } > "$SESSION_FILE"
  printf '{"type":"session","version":3,"id":"22222222-bbbb","timestamp":"%s","cwd":"x"}\n' "$T0" > "$SD/2026-10-02T00-00-00-000Z_22222222-bbbb.jsonl"
  mk_stop "$STEM" pi-cli "${2:-ended}" CONFIRMED '[]'
}

mk_pi_attempt p1
run_gate issue --stem "$STEM" --raw "$RAW" --session-dir "$SD"
check "Pi CLI: issue finds the attempt's session by the id on the raw stream's first line" "$([ "$RC" = "0" ] && [ "$(json_of "$STEM-evidence.json" files.1.kind)" = "transcript" ] && [ "$(has '11111111-aaaa' "$(json_of "$STEM-evidence.json" files.1.path)")" = "1" ] && echo 1 || echo 0)"
check "Pi CLI: only that session is listed" "$([ "$(json_of "$STEM-evidence.json" files.1.kind)" = "transcript" ] && [ "$(json_of "$STEM-evidence.json" files.2.kind)" = "" ] && echo 1 || echo 0)"
check "Pi CLI: the manifest takes the start from the session file" "$([ "$(json_of "$STEM-evidence.json" t0)" = "$(date -u -d "$T0" +%s)" ] && echo 1 || echo 0)"

mk_pi_attempt p2
run_gate issue --stem "$STEM"
usage_error "Pi CLI: issue needs the raw stream and the session directory"

mk_pi_attempt p3
printf '{"type":"turn_end"}\n' > "$RAW"
run_gate issue --stem "$STEM" --raw "$RAW" --session-dir "$SD"
refused "Pi CLI: a raw stream without a session id: no judge"

mk_pi_attempt p4
rm -f "$SESSION_FILE"
run_gate issue --stem "$STEM" --raw "$RAW" --session-dir "$SD"
refused "Pi CLI: no session file for the id: no judge"

mk_pi_attempt p5
run_gate issue --stem "$STEM" --raw "$CASE/raw/missing.jsonl" --session-dir "$SD"
refused "Pi CLI: a raw stream that does not exist: no judge"

mk_pi_attempt p6
STEM="$CASE/05-2"
mk_stop "$STEM" pi-cli ended CONFIRMED '[]'
run_gate issue --stem "$STEM" --raw "$RAW" --session-dir "$SD"
refused "Pi CLI: a stem that is not an attempt's first run: no judge"

issue_case c1
allow_record
ask
allowed "a record that allows a restart, within the evidence, is accepted: the next attempt keeps its deadline" 600
check "the answer names the record" "$(has 'judge.md' "$OUT")"

issue_case c2
allow_record
ask "deny $RECORD"
refused "a deny reply is refused"
check "the refusal says the judge denied" "$(has 'denied' "$ERR")"

issue_case c3
allow_record
ask "allow $CASE/another.md"
refused "a reply that names another record is refused"

issue_case c4
allow_record
ask "allow $RECORD and more"
refused "a reply with words after the record path is refused"

issue_case c5
allow_record
ask ""
refused "an empty reply is refused"

issue_case c6
allow_record
ask "approve $RECORD"
refused "a verb other than allow or deny is refused"

new_attempt c7
ask "allow $RECORD"
refused "no judge was issued: nothing to check"
check "the refusal says to issue first" "$(has 'issue' "$ERR")"

issue_case c8
ask
refused "the judge wrote no record: refused"

issue_case c9
allow_record
touch -d "@$(node -e 'const m=JSON.parse(require("fs").readFileSync(process.argv[1],"utf8"));process.stdout.write(String(Math.floor(m.judge.issued_at+m.judge.timeout)+60))' "$STEM-evidence.json")" "$RECORD"
ask
refused "a record written after the judge's deadline is refused"
check "the refusal names the deadline" "$(has 'deadline' "$ERR")"

issue_case c10
cat > "$RECORD" <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
refused "a record without the judge's end marker is refused"

issue_case c11
cat > "$RECORD" <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
[[MO-JUDGE-END another-judge]]
EOF
ask
refused "a record that ends in another judge's marker is refused"

issue_case c12
allow_record
printf 'one more line\n' >> "$RECORD"
ask
refused "a record with anything after the end marker is refused"

issue_case c13
mk_record <<EOF
verdict: deny
method: none
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
refused "a record that does not say verdict allow is refused, whatever the reply says"

issue_case c13b
mk_record <<EOF
verdict: deny
method: restart
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
refused "a record that says verdict deny is refused even when its method says restart"
check "the refusal names the verdict" "$(has 'verdict' "$ERR")"

issue_case c14
mk_record <<EOF
verdict: allow
method: none
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
refused "a record whose method is none is refused"

issue_case c15
mk_record <<EOF
verdict: allow
method: resume
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
refused "a method other than restart is refused: only a run from the start can be carried out"

issue_case c16
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2-L2: it was writing one file whole
EOF
ask
refused "a record that does not state its limits is refused"

issue_case c17
mk_record <<EOF
verdict: allow
method: restart
evidence:
limits: none
EOF
ask
refused "a record that cites nothing is refused"

issue_case c18
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $CASE/elsewhere.txt#L1-L1: not listed
limits: none
EOF
printf 'x\n' > "$CASE/elsewhere.txt"
ask
refused "a citation of a file outside the manifest is refused"

issue_case c19
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2-L9: past the end
limits: none
EOF
ask
refused "a citation past the last line is refused"

issue_case c20
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L3-L2: reversed
limits: none
EOF
ask
refused "a reversed range is refused"

issue_case c21
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L0-L1: before the first line
limits: none
EOF
ask
refused "line 0 is refused"

issue_case c22
mk_record <<EOF
verdict: allow
method: restart
evidence:
- just some words
limits: none
EOF
ask
refused "an evidence line that is not a citation is refused"

issue_case c23
allow_record
printf '{"type":"assistant","timestamp":"%s","message":{"content":[]}}\n' "$T1" >> "$TRANS"
ask
refused "a listed file that changed after it was listed voids the record"
check "the refusal names the file" "$(has 'changed' "$ERR")"

issue_case c24
allow_record
rm -f "$DELIV"
ask
refused "a listed file that is gone voids the record"

issue_case c25
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $DELIV#L1-L2: the report it had written
- $STEM-stop.json#L1-L1: the stop record
limits: none
EOF
ask
allowed "any listed file may be cited, the deliverable and the stop record included" 600

issue_case c26
mk_record <<EOF
verdict: allow
method: restart
evidence:
- $TRANS#L2: a single line
- $TRANS#L1-3: a range without the second L
limits: none
EOF
ask
allowed "a single line and a range written without the second L are both cited" 600

issue_case c27
allow_record
sed -i 's/$/\r/' "$RECORD"
ask
allowed "a record saved with CRLF line endings is read the same" 600

issue_case c28
mk_record <<EOF
verdict: allow
method: restart
next-timeout: 5
evidence:
- $TRANS#L2-L2: it was writing one file whole
limits: none
EOF
ask
allowed "after an attempt that only ended, a next-timeout in the record is not taken: the deadline stays" 600

if [ "$WINDOWS" = 1 ]; then
  issue_case c29
  MSYS_FORM="$(cygpath -u "$TRANS")"
  mk_record <<EOF
verdict: allow
method: restart
evidence:
- $MSYS_FORM#L2-L2: it was writing one file whole
limits: none
EOF
  ask
  allowed "a citation written as an MSYS path names the same file" 600

  issue_case c30
  BACKSLASH_FORM="$(cygpath -w "$TRANS")"
  mk_record <<EOF
verdict: allow
method: restart
evidence:
- $BACKSLASH_FORM#L2-L2: it was writing one file whole
limits: none
EOF
  ask
  allowed "a citation written with backslashes names the same file" 600
else
  skip "path forms are checked on Windows only"
fi

issue_case d1 deadline
deadline_record L2-L2 900
ask
allowed "after a deadline, a longer next-timeout backed by a line inside the attempt's run is accepted: the next attempt gets it" 900

issue_case d2 deadline
allow_record
ask
refused "after a deadline, a record with no next-timeout is refused"

issue_case d3 deadline
deadline_record L2-L2 600
ask
refused "after a deadline, a next-timeout equal to the deadline hit is refused"

issue_case d4 deadline
deadline_record L2-L2 300
ask
refused "after a deadline, a shorter next-timeout is refused"

issue_case d5 deadline
deadline_record L2-L2 abc
ask
refused "after a deadline, a next-timeout that is not an integer is refused"

issue_case d6 deadline
deadline_record L3-L3 900
ask
refused "after a deadline, the only cited transcript line stamped outside the attempt's run is refused"

new_attempt d7 deadline
printf '{"timestamp":"%s","note":"a stamped line that is not a transcript"}
' "$T1" > "$DELIV"
run_gate issue --stem "$STEM" --deliv "$DELIV"
SENTINEL="$(json_of "$STEM-evidence.json" judge.sentinel)"
mk_record <<EOF
verdict: allow
method: restart
next-timeout: 900
evidence:
- $DELIV#L1-L1: only the deliverable
limits: none
EOF
ask
refused "after a deadline, citing no transcript line is refused"

new_attempt d8 deadline
printf '{"type":"user","message":{"content":"no stamp"}}\n{"type":"assistant","message":{"content":[]}}\n' > "$TRANS"
run_gate issue --stem "$STEM" --deliv "$DELIV"
refused "after a deadline, a transcript with no stamps cannot back a longer deadline: no judge is issued"
check "no manifest or prompt is left behind" "$([ ! -e "$STEM-evidence.json" ] && [ ! -e "$STEM-judge-prompt.md" ] && echo 1 || echo 0)"

issue_case d9 deadline
deadline_record L1-L1 900
ask
allowed "a line stamped at the very start of the run counts as inside it" 900

issue_case d10 deadline
mk_record <<EOF
verdict: allow
method: restart
next-timeout: 900
evidence:
- $TRANS#L3-L3: late
- $TRANS#L2-L2: in the run
limits: none
EOF
ask
allowed "one cited line inside the run is enough, whatever else is cited" 900

echo
echo "passed $PASS / $((N - SKIP)) (skipped $SKIP)"
[ "$FAIL" -eq 0 ]
