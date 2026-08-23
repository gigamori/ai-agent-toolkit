#!/usr/bin/env bash
# Turn watchdog for mode-orchestrator.
#
# Purpose: bound a delegated turn in wall-clock time and detect a subagent that
# stops generating. The orchestrator is event-driven and cannot poll on its own;
# the only asynchronous wake path available to it is the completion of a
# background task it started itself. So this script runs under
# `Bash(run_in_background=true)` and its EXIT is the wake signal.
#
# Verdict is the single stdout word, and also the exit code:
#   TIMEOUT  1  wall-clock deadline reached
#   STALL    2  the subagent's transcript stopped growing for STALL seconds
#
# Writing the deliverable is NOT an exit condition. A subagent writes its file
# and only then composes its reply, so exiting on the file would (a) wake the
# orchestrator before the turn's own completion notification, with no defined
# meaning, and (b) leave the reply-composing tail of the turn with no time bound
# at all — the undetected wait this whole mechanism exists to prevent. The
# normal-path wake is the turn's own completion notification; the orchestrator
# stops this watchdog at that point. The deliverable is still observed, and both
# verdict messages state whether it was written, because a hung tail presents
# exactly as "deliverable written, then transcript idle".
#
# On TIMEOUT / STALL the orchestrator classifies the turn `aborted` (see
# SKILL.md, Execution step 4). This script never kills anything: stopping the
# subagent is the orchestrator's call, made with the harness's own task tools.
#
# Observed harness facts this depends on (measured 2026-07-28, claude v2.1.218,
# win32-x64) — re-verify before relying on them elsewhere:
#   - The session log directory does NOT exist when the turn starts; it appears
#     a few seconds after the delegation call (~3s measured). So the transcript
#     has to be resolved by polling, not once at startup.
#   - `<session>/subagents/agent-<agentId>.jsonl` IS appended live while the
#     subagent works, so its mtime is a valid liveness signal.
#   - `<session>/subagents/agent-<agentId>.meta.json` carries the `description`
#     passed on the delegation call, which is how this script resolves the
#     agentId without the orchestrator ever handling that id.
#   - `tasks/<agentId>.output` is 0 bytes even for a turn that ran to
#     completion. It is NOT a liveness signal and is deliberately unused here.
#   - A legitimate long tool call idles the transcript for the whole duration of
#     that call, so STALL must exceed the longest tool call a turn may make.
#   - A deliverable path is REUSED across attempts: SKILL.md re-runs an aborted
#     turn onto the same file. So "the file exists" cannot mean "this turn wrote
#     it"; the freshness stamp below is what makes the observation answerable
#     for this turn, and therefore what the verdict messages can rely on.

set -u

# ---------------------------------------------------------------------------
# Configuration — edit these, not the body.
# ---------------------------------------------------------------------------

# Seconds of transcript inactivity that count as a stalled subagent.
STALL=600

# Wall-clock budget per turn mode, in seconds.
DEADLINE_SURVEY=3600
DEADLINE_PLAN=2400
DEADLINE_EXECUTE=1500
DEADLINE_DEBUG=900
DEADLINE_REVIEW=900
DEADLINE_REVIEW_DEV=1800
DEADLINE_DEFAULT=900

# Seconds between checks.
POLL=15

# How long to keep trying to resolve the agentId before giving up on stall
# detection and continuing with deliverable + deadline only.
RESOLVE_GRACE=120

# How far before start time a subagent's meta.json may have been created and
# still be considered this turn's. The orchestrator starts this watchdog and the
# delegation call together and their order is not guaranteed, so a stamp taken
# strictly at start time can miss a meta.json that landed microseconds earlier —
# and once missed it is missed for good, since the filter never loosens. The
# window only has to be wide enough to cover that race; it stays far below the
# gap between two runs, so a previous run's identically-described turn cannot
# fall inside it.
RESOLVE_BACKDATE=120

# Where session logs live. Override with --project-root for a non-default setup.
PROJECT_ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects"

# ---------------------------------------------------------------------------

usage() {
  cat >&2 <<'EOF'
usage: watchdog.sh --deliv <path> --desc <delegation description> [options]

  --deliv <path>     Deliverable the turn must write. Watching it never ends the
                     run — it only enriches the verdict message and the trace
                     with whether the turn had written its file. Use `-` for a
                     turn that writes no file; the check is then skipped.
  --desc <text>      The description string passed on the delegation call, used
                     verbatim to resolve the agent's transcript. Must match
                     exactly. Keep it free of double quotes and backslashes.
  --mode <name>      Turn mode; selects the deadline. Default: unset -> default.
  --deadline <sec>   Override the mode's deadline.
  --stall <sec>      Override STALL.
  --poll <sec>       Override POLL.
  --project-root <p> Override the session-log root.
  --log <path>       Append a trace here (default: no trace).
EOF
  exit 64
}

DELIV=""
DESC=""
MODE=""
DEADLINE=""
TRACE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --deliv)        DELIV="${2-}"; shift 2 ;;
    --desc)         DESC="${2-}"; shift 2 ;;
    --mode)         MODE="${2-}"; shift 2 ;;
    --deadline)     DEADLINE="${2-}"; shift 2 ;;
    --stall)        STALL="${2-}"; shift 2 ;;
    --poll)         POLL="${2-}"; shift 2 ;;
    --project-root) PROJECT_ROOT="${2-}"; shift 2 ;;
    --log)          TRACE="${2-}"; shift 2 ;;
    -h|--help)      usage ;;
    *) echo "watchdog: unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$DELIV" ] || { echo "watchdog: --deliv is required" >&2; usage; }
[ -n "$DESC" ]  || { echo "watchdog: --desc is required" >&2; usage; }

if [ -z "$DEADLINE" ]; then
  case "$MODE" in
    survey)     DEADLINE=$DEADLINE_SURVEY ;;
    plan)       DEADLINE=$DEADLINE_PLAN ;;
    execute)    DEADLINE=$DEADLINE_EXECUTE ;;
    debug)      DEADLINE=$DEADLINE_DEBUG ;;
    review)     DEADLINE=$DEADLINE_REVIEW ;;
    review-dev) DEADLINE=$DEADLINE_REVIEW_DEV ;;
    *)          DEADLINE=$DEADLINE_DEFAULT ;;
  esac
fi

trace() {
  [ -n "$TRACE" ] || return 0
  echo "[$(date -u +%H:%M:%S)] $*" >> "$TRACE"
}

# Emit the verdict on stdout, with the reason on stderr, then exit.
verdict() {
  echo "$1"
  echo "watchdog: $1 — $2" >&2
  trace "$1 — $2"
  case "$1" in
    TIMEOUT) exit 1 ;;
    STALL)   exit 2 ;;
  esac
  exit 3
}

# Both verdicts carry this. A tail-hang — the subagent wrote its file and then
# stopped generating mid-reply — is only legible if the STALL message says the
# deliverable was already there.
deliv_note() {
  if [ "$DELIV" = "-" ]; then
    echo "no deliverable expected"
  elif [ -n "$DELIV_AT" ]; then
    echo "deliverable written ${DELIV_AT}s in"
  else
    echo "no deliverable written this turn"
  fi
}

# Locate the transcript of the agent whose meta.json carries our description.
# Searched across the whole session-log root so the caller never has to know the
# session id, and matched on the exact JSON field so a description that is a
# prefix of another turn's cannot collide. Only files touched at or after t0 are
# eligible, which keeps an identical description from an earlier run out.
resolve_transcript() {
  local needle meta
  needle="\"description\":\"$DESC\""
  while IFS= read -r meta; do
    [ -n "$meta" ] || continue
    if grep -qF "$needle" "$meta" 2>/dev/null; then
      echo "${meta%.meta.json}.jsonl"
      return 0
    fi
  done <<EOF
$(find "$PROJECT_ROOT" -maxdepth 4 -name 'agent-*.meta.json' -newer "$STAMP" 2>/dev/null)
EOF
  return 1
}

t0=$(date +%s)

STAMP="$(mktemp)"
DELIV_STAMP="$(mktemp)"
trap 'rm -f "$STAMP" "$DELIV_STAMP"' EXIT
touch -d "@$((t0 - RESOLVE_BACKDATE))" "$STAMP" 2>/dev/null || true

# The deliverable stamp is t0 exactly, with NO backdating. `meta.json` gets a
# grace window because the delegation call and this script start together and
# their order is not guaranteed; a deliverable has no such race, since the turn
# cannot have written its output before it started. Widening this window would
# re-admit the previous attempt's leftover file — a re-run's deliverable path is
# the SAME path — and the verdict message would then report someone else's file
# as this turn's output.
touch -d "@$t0" "$DELIV_STAMP" 2>/dev/null || true

TRANS=""
STALE_NOTED=""
# Elapsed seconds at which this turn's deliverable was first seen. Latched: it
# records the observation, it does not end the run.
DELIV_AT=""
trace "start deliv=$DELIV mode=${MODE:-<unset>} deadline=${DEADLINE}s stall=${STALL}s poll=${POLL}s"

while :; do
  now=$(date +%s)
  elapsed=$((now - t0))

  if [ "$DELIV" != "-" ] && [ -z "$DELIV_AT" ] && [ -s "$DELIV" ]; then
    if [ "$DELIV" -nt "$DELIV_STAMP" ]; then
      # Not an exit. The turn still has to compose its reply, and that tail is
      # exactly what stays unbounded if the watchdog leaves here.
      DELIV_AT=$elapsed
      trace "deliverable written after ${elapsed}s — continuing to monitor: $DELIV"
    elif [ -z "$STALE_NOTED" ]; then
      # Left by an earlier attempt at this same turn. Say so once: a run that
      # keeps starting against a stale deliverable is worth seeing in the trace,
      # and silence here is what made the original defect invisible.
      STALE_NOTED=1
      trace "deliverable exists but is not newer than t0 — stale, ignoring: $DELIV"
    fi
  fi

  if [ -z "$TRANS" ] && [ "$elapsed" -lt "$RESOLVE_GRACE" ]; then
    if TRANS="$(resolve_transcript)"; then
      trace "resolved transcript after ${elapsed}s: $TRANS"
    else
      TRANS=""
    fi
  fi

  if [ -n "$TRANS" ] && [ -e "$TRANS" ]; then
    mtime=$(stat -c %Y "$TRANS" 2>/dev/null || echo "$now")
    idle=$((now - mtime))
    trace "elapsed=${elapsed}s idle=${idle}s size=$(stat -c %s "$TRANS" 2>/dev/null || echo '?')"
    if [ "$idle" -ge "$STALL" ]; then
      verdict STALL "no transcript activity for ${idle}s (elapsed ${elapsed}s) — $(deliv_note)"
    fi
  else
    trace "elapsed=${elapsed}s (no transcript yet)"
  fi

  if [ "$elapsed" -ge "$DEADLINE" ]; then
    verdict TIMEOUT "deadline of ${DEADLINE}s reached — $(deliv_note)"
  fi

  sleep "$POLL"
done
