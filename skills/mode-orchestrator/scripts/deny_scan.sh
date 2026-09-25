#!/usr/bin/env bash
# deny_scan.sh — mechanical permission-denial check for one finished turn.
#
# Why this exists (reliability-spec.md §8.1 defect 4): the reply contract
# makes any permission denial `blocked`, but the status line is the turn's
# self-report — a real run returned `ok` after judging its denied `rm -f`
# inessential. The denial *is* machine-visible: the subagent transcript
# records it as a tool_result with `"is_error":true` and, on the same line, a
# `"toolDenialKind"` field; transcripts older than that field carry only a
# stable phrase (first measured on the incident transcript: "Permission for
# this action was denied by the Claude Code auto mode classifier"). This
# script greps for both, so the orchestrator can override a self-reported
# `ok` with `blocked` on evidence instead of trust.
#
# Run it once per turn, after the turn's status line has been read (unlike
# the watchdog it is not a race — the transcript is complete by then).
# Claude Code only: Pi has no permission layer, so no denial can occur there
# (see references/harness-pi.md).
#
# Output (stdout is the verdict, like watchdog.sh):
#   CLEAN          exit 0 — no denial in the turn's transcript
#   DENIED <n>     exit 1 — n denial results found: the turn's status is
#                  `blocked` regardless of what its reply said
#   NO-TRANSCRIPT  exit 2 — the description resolved no transcript, or an
#                  empty one; the check could not run (fail-visible: say so
#                  in the run index, do not silently treat it as CLEAN)
#
# --out <file> writes the same verdict to <file> as its first line, followed
# by the description, the transcript scanned and the scan time. A verdict
# that exists only on stdout is lost whenever the harness drops the tool
# result from the session log: 13 calls in one run (2026-09-17) left no
# result there, and nothing showed what they had printed. A failed write is
# reported on stderr and changes neither the verdict nor the exit code.
#
# Resolution reuses the watchdog's key: the delegation call's description,
# matched verbatim against agent-*.meta.json. Descriptions are unique per
# delegation by contract (SKILL.md), so at most one live match exists; if
# several files match anyway (e.g. leftovers from an older run reusing the
# string against contract), the most recently modified one is taken.

set -u

# Denial patterns, matched as FIXED STRINGS (grep -F) and only on lines that
# also carry "is_error":true. That guard does not exclude program output: a
# failing Bash command's stdout is written to an "is_error":true line too, so
# a turn that greps or diffs a file containing these phrases trips the scan
# (measured 2026-09-23: one such line on this machine, a `diff` of this
# script). The structural field below cannot be tripped that way.
#
# Every entry here must be MEASURED on a real transcript, never guessed: a
# pattern that matches nothing is indistinguishable from a run with no denial,
# and this script fails open. Keep superseded phrases rather than replacing
# them — old logs stay scannable and a reverted wording still trips the scan.
#
#   1. Auto-mode classifier wording, measured on the defect-4 incident
#      transcript.
#   2. Manual-deny wording; UNVERIFIED against a real manual deny in this
#      repo's logs so far.
#   3. Current template family, measured 2026-08-12 on two independent
#      transcripts from the decision-point E2E (both real Bash denials that
#      patterns 1-2 missed entirely — see the drift note below). The two
#      observed texts were
#        "Error: Permission to use Bash has been denied. IMPORTANT: ..."
#        "Error: Permission to use Bash with command hostname has been denied."
#      so the tool name and an optional " with command <cmd>" clause both vary.
#      The pattern is therefore the invariant prefix, deliberately stopping
#      before the tool name so an Edit/Write/WebFetch denial trips it too. It
#      is NOT narrowed to Bash: this repo has only ever measured Bash denials,
#      and over-fitting to that sample is how patterns 1-2 went stale.
#
# DRIFT INCIDENT 2026-08-12: patterns 1-2 detected nothing on either of the
# two denials above while `"is_error":true` still matched, i.e. only the
# phrasing moved. Both turns happened to self-report `blocked` honestly, so
# nothing was misclassified — but the machine backstop that exists precisely
# for a turn that does NOT self-report was inert. `deny_scan_test.sh` stayed
# green throughout, because fixed fixtures cannot notice the real format
# changing underneath them. That is why the canary in harness-cc.md is a
# manual step, and why it is worth running.
PATTERNS=(
  'Permission for this action was denied'
  "The user doesn't want to proceed"
  'Permission to use '
)

# Structural denial marker. Claude Code writes it on the denied call's own
# tool_result line, next to "is_error":true, whatever refused the call: a
# permission rule, a PreToolUse hook's own reason text, the auto-mode
# classifier, or a permission prompt that was not approved. It is the
# primary key; the PATTERNS above remain for transcripts written before the
# field existed. Observed shapes and dates: docs/dev/test-constraints.md.
#
# DRIFT INCIDENT 2026-09-22: a PreToolUse hook refused a Grep in three turns
# of one run with its own reason text, which no pattern matched. All three
# scanned CLEAN; two of those turns had self-reported `ok`, and the run went
# on past both. A phrase list cannot know a hook's wording in advance; the
# field does not depend on wording at all.
#
# Every kind counts, `cancelled` (the call was interrupted) and
# `automode-unavailable` (the classifier could not be reached) included:
# each is a call the turn asked for that did not run (ruled 2026-09-25).
DENIAL_FIELD='"toolDenialKind":"'

PROJECT_ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"

usage() {
  cat >&2 <<'EOF'
usage: deny_scan.sh --desc <delegation description> [--out <file>] [--project-root <p>]

  --desc <text>      The description string passed on the delegation call,
                     used verbatim to resolve the agent's transcript.
  --out <file>       Also write the verdict, the description, the transcript
                     scanned and the scan time to <file>.
  --project-root <p> Override the session-log root.
EOF
  exit 64
}

DESC=""
OUT=""
# A flag given last, with no value, is a usage error: `shift 2` would leave
# the arguments unchanged and this loop would never end.
while [ $# -gt 0 ]; do
  case "$1" in
    --desc)         [ $# -ge 2 ] || usage; DESC="$2"; shift 2 ;;
    --out)          [ $# -ge 2 ] || usage; OUT="$2"; shift 2 ;;
    --project-root) [ $# -ge 2 ] || usage; PROJECT_ROOT="$2"; shift 2 ;;
    -h|--help)      usage ;;
    *) echo "deny_scan: unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$DESC" ] || { echo "deny_scan: --desc is required" >&2; usage; }

# File mtime, portable across GNU coreutils (stat -c) and BSD/macOS
# (stat -f). Either failing falls through to 0, which only matters when
# several meta files match the same description — a contract violation to
# begin with; the unique-description case never compares mtimes.
mtime_of() {
  stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null || echo 0
}

# The --out record. Its first line is the verdict exactly as stdout prints
# it, so `head -n 1` reads it back; the rest says what was scanned and when.
write_out() {
  [ -n "$OUT" ] || return 0
  if ! {
    echo "$1"
    echo "desc: $DESC"
    echo "transcript: ${transcript:--}"
    [ -z "$kinds" ] || echo "kinds:$kinds"
    echo "scanned: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } 2>/dev/null > "$OUT"; then
    echo "deny_scan: could not write --out $OUT" >&2
  fi
}

# One grep over the whole candidate set, not one per file. The per-file loop
# spawned a grep for every meta.json on the machine — 4793 of them at ~15.6 ms
# each, ~73 s per call (measured 2026-09-23); the find alone takes 0.4 s.
# Only matching files reach the loop, so mtime_of runs once per match.
needle="\"description\":\"$DESC\""
transcript=""
kinds=""
best_mtime=0
while IFS= read -r meta; do
  [ -n "$meta" ] || continue
  mtime=$(mtime_of "$meta")
  if [ "$mtime" -ge "$best_mtime" ]; then
    best_mtime=$mtime
    transcript="${meta%.meta.json}.jsonl"
  fi
done <<EOF
$(find "$PROJECT_ROOT" -maxdepth 4 -name 'agent-*.meta.json' -print0 2>/dev/null \
  | xargs -0 -r grep -lF -- "$needle" 2>/dev/null)
EOF

# -s as well as -f: a resolved 0-byte transcript would otherwise scan CLEAN,
# indistinguishable from a turn with no denial.
if [ -z "$transcript" ] || [ ! -f "$transcript" ] || [ ! -s "$transcript" ]; then
  write_out "NO-TRANSCRIPT"
  echo "NO-TRANSCRIPT"
  echo "deny_scan: no usable transcript for --desc \"$DESC\"${transcript:+ (resolved $transcript, missing or empty)}" >&2
  exit 2
fi

# One pass over the "is_error":true lines, matching the structural field or
# any phrase, so a line that trips several needles counts once. -a on every
# grep: without it a transcript holding a NUL byte is answered with the
# single line "Binary file ... matches", which carries no needle, and the
# verdict comes out CLEAN.
needles="$DENIAL_FIELD"
for pat in "${PATTERNS[@]}"; do
  needles="$needles
$pat"
done
count=$(grep -aF '"is_error":true' "$transcript" 2>/dev/null | grep -caF -- "$needles" || true)

if [ "$count" -gt 0 ]; then
  kinds=$(grep -aF '"is_error":true' "$transcript" 2>/dev/null \
    | grep -ao '"toolDenialKind":"[^"]*"' | sed 's/.*:"//; s/"$//' \
    | sort | uniq -c | awk '{printf " %s=%s", $2, $1}')
  write_out "DENIED $count"
  echo "DENIED $count"
  echo "deny_scan: $count denial tool_result(s) in $transcript${kinds:+ --${kinds}}" >&2
  exit 1
fi
write_out "CLEAN"
echo "CLEAN"
exit 0
