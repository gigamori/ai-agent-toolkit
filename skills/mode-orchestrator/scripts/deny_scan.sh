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

# Denial patterns, matched only on lines that also carry "is_error":true —
# an agent that merely *prints* or *reads* this text does not trip the scan,
# since that content lands in other entry kinds. First phrase measured on the
# defect-4 incident transcript (auto-mode classifier). The second is Claude
# Code's manual-deny wording; kept as a second net, UNVERIFIED against a real
# manual deny in this repo's logs so far.
PATTERNS=(
  'Permission for this action was denied'
  "The user doesn't want to proceed"
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

needle="\"description\":\"$DESC\""
transcript=""
best_mtime=0
while IFS= read -r meta; do
  [ -n "$meta" ] || continue
  if grep -qF "$needle" "$meta" 2>/dev/null; then
    mtime=$(stat -c %Y "$meta" 2>/dev/null || echo 0)
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
