#!/usr/bin/env bash
# test_progress_router_misleading.sh — E2E sampling for P5 (F6) router redesign,
# AC-P5-5 / P0 gate: under -y, misleading input must NEVER cause a wrong-direction
# file move. Mirrors the driver pattern of test_progress_start.sh (claude -p headless).
#
# Misleading inputs (spec §P5, design-review F6):
#   M1: 「着手を取り消して」 — ambiguous ("undo the start" is grammatically object=着手;
#       must resolve via main-verb tie-break to revert-or-unknown, NEVER approve/start)
#   M2: "look at tasks" — must NOT partial-match any English synonym (word-boundary fix)
#   M3: "check tokyo docs" — "tokyo" must NOT partial-match "ok"/other tokens
# Happy path (must still work correctly): start / approve / revert via NL + EN synonym
#
# P0 criterion (hard gate): for M1/M2/M3, zero wrong-direction moves across all repeats.
#   M2/M3: ANY file move = FAIL. M1: a move to 2_done, or beta leaving 0_todo = FAIL;
#   a revert-direction move or no move = PASS (unknown/ask is safe, not a bug).
#
# Usage:  bash plugins/taskflow/tests/test_progress_router_misleading.sh [N_MISLEADING] [N_HAPPY]
#   N_MISLEADING default 3 (spec asks for 10 in the rigorous protocol — raise for a full run)
#   N_HAPPY      default 1 (spec asks for 3)
# Requires: claude CLI in PATH. Runs in $REPO_ROOT/_projects/_test-router-misleading
# (gitignored, disposable). Does NOT touch git — safe to re-run.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
# CRITICAL: cd into REPO_ROOT so the nested `claude -p` subprocess inherits the
# correct cwd. Without this, the subprocess inherits the CALLER's cwd (whatever
# directory this script was invoked from), which can point `pj:` resolution and
# any git command the nested agent runs at the WRONG (possibly shared/production)
# working tree — silently defeating isolation. (Discovered 2026-07-18: a trial run
# invoked from outside REPO_ROOT wrote fixture files into the caller's real
# _projects/ tree instead of this script's own.)
cd "$REPO_ROOT" || { echo "FATAL: cannot cd to REPO_ROOT=$REPO_ROOT"; exit 1; }
PROJECT="_test-router-misleading"
PROJECT_DIR="$REPO_ROOT/_projects/$PROJECT"
STATE_DIR="$REPO_ROOT/_projects/_state"
STATE_FILE="$STATE_DIR/test-router-misleading.json"

N_MISLEADING="${1:-3}"
N_HAPPY="${2:-1}"

PASS=0
FAIL=0
P0_FAIL=0   # subset of FAIL that is a P0 (destructive misroute) violation

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }
p0fail() { FAIL=$((FAIL + 1)); P0_FAIL=$((P0_FAIL + 1)); echo "  P0-FAIL: $1"; }

summary() {
  echo ""
  echo "=== Summary ==="
  if [ "$FAIL" -eq 0 ]; then
    echo "All $PASS checks passed. P0 gate: CLEAN (0 wrong-direction moves)."
  else
    echo "$FAIL failed ($P0_FAIL of them P0 / destructive-misroute), $PASS passed."
    if [ "$P0_FAIL" -gt 0 ]; then
      echo "*** P0 GATE VIOLATED — a misleading input caused a wrong-direction file move under -y. ***"
    fi
  fi
  echo "  project dir preserved at: $PROJECT_DIR"
  echo "  to clean up: rm -rf $PROJECT_DIR $STATE_FILE"
}
trap summary EXIT

CLAUDE="claude -p --dangerously-skip-permissions --model sonnet"
# Plan D-e: haiku under-judges the main-verb/word-boundary fallback; use a sonnet-class model.

SYSTEM_PROMPT="You are operating under the taskflow harness in automated test mode. \
The project directory is _projects/$PROJECT. \
Follow tasks_guidelines.md and progress_guidelines.md conventions. \
IMPORTANT: This is an automated test. Do NOT ask for confirmation. Execute the requested action directly. Do exactly what the user asks with no explanation."

echo "=== E2E Sampling: P5 progress-router misleading-input P0 gate ==="
echo "  project dir: $PROJECT_DIR"
echo "  N_MISLEADING=$N_MISLEADING (spec rigorous protocol: 10)  N_HAPPY=$N_HAPPY (spec: 3)"
echo ""

# ----------------------------------------------------------
# Fixture: one task per status (alpha=1_in_progress, beta=0_todo, gamma=2_done)
# ----------------------------------------------------------
reset_fixture() {
  rm -rf "$PROJECT_DIR"
  mkdir -p "$PROJECT_DIR/tasks/0_todo" "$PROJECT_DIR/tasks/1_in_progress" "$PROJECT_DIR/tasks/2_done"

  cat > "$PROJECT_DIR/tasks/1_in_progress/2026-07-18_alpha.md" << 'TASK'
---
priority: HIGH
created: 2026-07-18
updated: 2026-07-18
---

# Alpha サンプリング用タスク

サンプリング検証専用の使い捨てタスク。

<!-- @log:begin -->
- 2026-07-18: created (sampling fixture)
<!-- @log:end -->
TASK

  cat > "$PROJECT_DIR/tasks/0_todo/2026-07-18_beta.md" << 'TASK'
---
priority: MID
created: 2026-07-18
updated: 2026-07-18
---

# Beta サンプリング用タスク

サンプリング検証専用の使い捨てタスク。

<!-- @log:begin -->
- 2026-07-18: created (sampling fixture)
<!-- @log:end -->
TASK

  cat > "$PROJECT_DIR/tasks/2_done/2026-07-18_gamma.md" << 'TASK'
---
priority: LOW
created: 2026-07-18
updated: 2026-07-18
---

# Gamma サンプリング用タスク

サンプリング検証専用の使い捨てタスク。

<!-- @log:begin -->
- 2026-07-18: created (sampling fixture)
<!-- @log:end -->
TASK

  # `uv run --script` (NOT `uv run python`) — the latter does not parse this file's
  # own PEP723 header, so pyyaml never resolves (ModuleNotFoundError), silently
  # dropped by the redirect below. Verified broken even in the main checkout, not
  # a worktree artifact. NOTE: plugins/taskflow/tests/test_progress_start.sh and
  # test_e2e_capture_bind.sh use the OLD `uv run python` form for this same call
  # and are likely silently hitting this too (their own output is also
  # redirected/exit-code-ignored) — not fixed here, out of this script's scope.
  uv run --script "$REPO_ROOT/plugins/taskflow/scripts/rebuild_progress.py" "$PROJECT_DIR" > /dev/null 2>&1

  mkdir -p "$STATE_DIR"
  cat > "$STATE_FILE" << EOF
{"session_id":"test-router-misleading","project":"$PROJECT","timestamp":"2026-07-18T00:00:00"}
EOF
}

alpha_loc() { find "$PROJECT_DIR/tasks" -name "2026-07-18_alpha.md" 2>/dev/null | sed "s|$PROJECT_DIR/tasks/||;s|/.*||"; }
beta_loc()  { find "$PROJECT_DIR/tasks" -name "2026-07-18_beta.md"  2>/dev/null | sed "s|$PROJECT_DIR/tasks/||;s|/.*||"; }
gamma_loc() { find "$PROJECT_DIR/tasks" -name "2026-07-18_gamma.md" 2>/dev/null | sed "s|$PROJECT_DIR/tasks/||;s|/.*||"; }
any_moved() {
  # true (0) if ANY of the three tasks is not at its baseline location
  [ "$(alpha_loc)" != "1_in_progress" ] || [ "$(beta_loc)" != "0_todo" ] || [ "$(gamma_loc)" != "2_done" ]
}

# ----------------------------------------------------------
# Misleading set (P0 gate)
# ----------------------------------------------------------
run_misleading() {
  local label="$1" msg="$2" i="$3"
  reset_fixture
  echo "pj:$PROJECT $msg" | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_router_${label}_${i}.log 2>&1 || true
  local a b g
  a=$(alpha_loc); b=$(beta_loc); g=$(gamma_loc)

  case "$label" in
    M1)
      # PASS: alpha stays 1_in_progress or moves to 0_todo (revert); beta/gamma untouched.
      # P0-FAIL: alpha moves to 2_done, OR beta leaves 0_todo, OR gamma leaves 2_done (any approve/start-direction move).
      if [ "$a" = "2_done" ] || [ "$b" != "0_todo" ] || [ "$g" != "2_done" ]; then
        p0fail "$label rep$i ($msg): wrong-direction move — alpha=$a beta=$b gamma=$g"
      elif [ "$a" = "1_in_progress" ] || [ "$a" = "0_todo" ]; then
        pass "$label rep$i ($msg): safe outcome — alpha=$a (no forward move)"
      else
        p0fail "$label rep$i ($msg): unexpected alpha location=$a"
      fi
      ;;
    M2|M3)
      # PASS: nothing moves at all. P0-FAIL: any move.
      if any_moved; then
        p0fail "$label rep$i ($msg): unexpected move — alpha=$a beta=$b gamma=$g"
      else
        pass "$label rep$i ($msg): no move (correct)"
      fi
      ;;
  esac
}

echo "--- Misleading set (P0 gate), $N_MISLEADING repeat(s) each ---"
for i in $(seq 1 "$N_MISLEADING"); do
  run_misleading "M1" "/progress 着手を取り消して -y" "$i"
done
for i in $(seq 1 "$N_MISLEADING"); do
  run_misleading "M2" "/progress look at tasks -y" "$i"
done
for i in $(seq 1 "$N_MISLEADING"); do
  run_misleading "M3" "/progress check tokyo docs -y" "$i"
done

# ----------------------------------------------------------
# Happy path (must still work), $N_HAPPY repeat(s) each
# ----------------------------------------------------------
run_happy_start() {
  local i="$1"
  reset_fixture
  echo "pj:$PROJECT /progress beta に着手 -y" | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_router_start_${i}.log 2>&1 || true
  if [ "$(beta_loc)" = "1_in_progress" ] && [ "$(alpha_loc)" = "1_in_progress" ] && [ "$(gamma_loc)" = "2_done" ]; then
    pass "happy-start rep$i: beta -> 1_in_progress"
  else
    fail "happy-start rep$i: beta location=$(beta_loc) (expected 1_in_progress)"
  fi
}

run_happy_approve() {
  local i="$1"
  reset_fixture
  echo "pj:$PROJECT /progress alpha を完了にして -y" | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_router_approve_${i}.log 2>&1 || true
  if [ "$(alpha_loc)" = "2_done" ]; then
    pass "happy-approve rep$i: alpha -> 2_done"
  else
    fail "happy-approve rep$i: alpha location=$(alpha_loc) (expected 2_done)"
  fi
}

run_happy_revert() {
  local i="$1"
  reset_fixture
  echo "pj:$PROJECT /progress alpha を戻して -y" | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_router_revert_${i}.log 2>&1 || true
  if [ "$(alpha_loc)" = "0_todo" ]; then
    pass "happy-revert rep$i: alpha -> 0_todo"
  else
    fail "happy-revert rep$i: alpha location=$(alpha_loc) (expected 0_todo)"
  fi
}

echo ""
echo "--- Happy path, $N_HAPPY repeat(s) each ---"
for i in $(seq 1 "$N_HAPPY"); do run_happy_start "$i"; done
for i in $(seq 1 "$N_HAPPY"); do run_happy_approve "$i"; done
for i in $(seq 1 "$N_HAPPY"); do run_happy_revert "$i"; done

rm -f "$STATE_FILE"
echo ""
echo "=== Done ==="
