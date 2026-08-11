#!/usr/bin/env bash
# deny_scan.sh — mechanical permission-denial check for one finished turn.
#
# Why this exists (reliability-spec.md §8.1 defect 4): the reply contract
# makes any permission denial `blocked`, but the status line is the turn's
# self-report — a real run returned `ok` after judging its denied `rm -f`
# inessential. The denial *is* machine-visible: the subagent transcript
# records it as a tool_result with `"is_error":true` whose text starts with
# a stable phrase (measured on the incident transcript:
# "Permission for this action was denied by the Claude Code auto mode
# classifier"). This script greps for exactly that, so the orchestrator can
# override a self-reported `ok` with `blocked` on evidence instead of trust.
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
#   NO-TRANSCRIPT  exit 2 — the description resolved no transcript; the check
#                  could not run (fail-visible: say so in the run index, do
#                  not silently treat it as CLEAN)
#
# Resolution reuses the watchdog's key: the delegation call's description,
# matched verbatim against agent-*.meta.json. Descriptions are unique per
# delegation by contract (SKILL.md), so at most one live match exists; if
# several files match anyway (e.g. leftovers from an older run reusing the
# string against contract), the most recently modified one is taken.

set -u

# Denial patterns, matched as FIXED STRINGS (grep -F) and only on lines that
# also carry "is_error":true — an agent that merely *prints* or *reads* this
# text does not trip the scan, since that content lands in other entry kinds.
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

PROJECT_ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"

usage() {
  cat >&2 <<'EOF'
usage: deny_scan.sh --desc <delegation description> [--project-root <p>]

  --desc <text>      The description string passed on the delegation call,
                     used verbatim to resolve the agent's transcript.
  --project-root <p> Override the session-log root.
EOF
  exit 64
}

DESC=""
while [ $# -gt 0 ]; do
  case "$1" in
    --desc)         DESC="${2-}"; shift 2 ;;
    --project-root) PROJECT_ROOT="${2-}"; shift 2 ;;
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

needle="\"description\":\"$DESC\""
transcript=""
best_mtime=0
while IFS= read -r meta; do
  [ -n "$meta" ] || continue
  if grep -qF "$needle" "$meta" 2>/dev/null; then
    mtime=$(mtime_of "$meta")
    if [ "$mtime" -ge "$best_mtime" ]; then
      best_mtime=$mtime
      transcript="${meta%.meta.json}.jsonl"
    fi
  fi
done <<EOF
$(find "$PROJECT_ROOT" -maxdepth 4 -name 'agent-*.meta.json' 2>/dev/null)
EOF

if [ -z "$transcript" ] || [ ! -f "$transcript" ]; then
  echo "NO-TRANSCRIPT"
  echo "deny_scan: no transcript resolved for --desc \"$DESC\"" >&2
  exit 2
fi

count=0
for pat in "${PATTERNS[@]}"; do
  n=$(grep -F '"is_error":true' "$transcript" 2>/dev/null | grep -cF "$pat" || true)
  count=$((count + n))
done

if [ "$count" -gt 0 ]; then
  echo "DENIED $count"
  echo "deny_scan: $count denial tool_result(s) in $transcript" >&2
  exit 1
fi
echo "CLEAN"
exit 0
