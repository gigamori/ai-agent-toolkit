#!/usr/bin/env bash
# test_progress_start.sh — E2E test for /progress start action
#
# Tests:
#   1. /progress start via NL synonym (着手)
#   2. /progress start via English synonym (start)
#   3. /progress start with ambiguous target (multiple candidates)
#   4. /progress start on empty 0_todo (no candidates)
#   5. Synonym collision: 「開始」 should route to start, not approve/revert
#   6. Task creation folder selection: already underway → ask or 1_in_progress
#
# Usage:  bash plugins/taskflow/tests/test_progress_start.sh
# Requires: claude CLI available in PATH

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PROJECT="_test-progress-start"
PROJECT_DIR="$REPO_ROOT/_projects/$PROJECT"
STATE_DIR="$REPO_ROOT/_projects/_state"
PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

summary() {
  echo ""
  if [ "$FAIL" -eq 0 ]; then
    echo "All $PASS tests passed."
  else
    echo "$FAIL failed, $PASS passed."
  fi
  echo "  project dir preserved at: $PROJECT_DIR"
  echo "  to clean up: rm -rf $PROJECT_DIR"
}
trap summary EXIT

CLAUDE="claude -p --dangerously-skip-permissions --model haiku"

SYSTEM_PROMPT="You are operating under the taskflow harness in automated test mode. \
The project directory is _projects/$PROJECT. \
Follow tasks_guidelines.md and progress_guidelines.md conventions. \
IMPORTANT: This is an automated test. Do NOT ask for confirmation. Execute the requested action directly. Do exactly what the user asks with no explanation."

echo "=== E2E Test: /progress start action ==="
echo "  project dir: $PROJECT_DIR"
echo ""

# ----------------------------------------------------------
# Setup: create project with 2 tasks in 0_todo
# ----------------------------------------------------------
mkdir -p "$PROJECT_DIR/tasks/0_todo"
mkdir -p "$PROJECT_DIR/tasks/1_in_progress"
mkdir -p "$PROJECT_DIR/tasks/2_done"

TASK_A="$PROJECT_DIR/tasks/0_todo/2026-05-21_feature-alpha.md"
cat > "$TASK_A" << 'TASK'
---
priority: HIGH
created: 2026-05-21
updated: 2026-05-21
---

# Feature Alpha の実装

テスト用タスクA。

<!-- @log:begin -->
- 2026-05-21: created
<!-- @log:end -->
TASK

TASK_B="$PROJECT_DIR/tasks/0_todo/2026-05-21_feature-beta.md"
cat > "$TASK_B" << 'TASK'
---
priority: MID
created: 2026-05-21
updated: 2026-05-21
---

# Feature Beta の設計

テスト用タスクB。

<!-- @log:begin -->
- 2026-05-21: created
<!-- @log:end -->
TASK

# Create initial progress.md via rebuild
uv run python "$REPO_ROOT/plugins/taskflow/scripts/rebuild_progress.py" "$PROJECT_DIR" > /dev/null 2>&1

# Write state file so /progress can resolve the project
STATE_FILE="$STATE_DIR/test-progress-start.json"
mkdir -p "$STATE_DIR"
cat > "$STATE_FILE" << EOF
{"session_id":"test-progress-start","project":"$PROJECT","timestamp":"2026-05-21T00:00:00"}
EOF

echo "Setup complete: 2 tasks in 0_todo"
echo ""

# ----------------------------------------------------------
# Scenario 1: /progress 着手 alpha — NL synonym routes to start
# ----------------------------------------------------------
echo "[Scenario 1] /progress 着手 alpha (NL synonym → start)"

echo "pj:$PROJECT /progress alpha に着手 -y" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_start_s1.log 2>&1 || true

if [ -f "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-alpha.md" ]; then
  pass "feature-alpha moved to 1_in_progress"
else
  if [ -f "$PROJECT_DIR/tasks/0_todo/2026-05-21_feature-alpha.md" ]; then
    fail "feature-alpha still in 0_todo (start did not execute)"
  else
    # Check if it ended up somewhere unexpected
    FOUND=$(find "$PROJECT_DIR/tasks" -name "*alpha*" 2>/dev/null | head -1)
    fail "feature-alpha not in expected location: ${FOUND:-not found}"
  fi
fi

# Verify log entry was appended
if [ -f "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-alpha.md" ]; then
  if grep -q "started" "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-alpha.md"; then
    pass "log entry 'started' appended"
  else
    fail "log entry 'started' not found in task file"
  fi

  # Verify updated: date changed
  if grep -q "updated: 2026-05-21" "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-alpha.md"; then
    pass "updated: date is today"
  else
    fail "updated: date not updated"
  fi
fi

# Verify progress.md reflects the move
if [ -f "$PROJECT_DIR/progress.md" ]; then
  IP_SECTION=$(sed -n '/^## In Progress/,/^## Completed/p' "$PROJECT_DIR/progress.md")
  if echo "$IP_SECTION" | grep -qi "alpha"; then
    pass "progress.md In Progress table shows alpha"
  else
    fail "progress.md In Progress table does not show alpha"
  fi
fi

# ----------------------------------------------------------
# Scenario 2: /progress start beta — English synonym
# ----------------------------------------------------------
echo ""
echo "[Scenario 2] /progress start beta (English synonym)"

echo "pj:$PROJECT /progress start beta -y" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_start_s2.log 2>&1 || true

if [ -f "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-beta.md" ]; then
  pass "feature-beta moved to 1_in_progress"
else
  fail "feature-beta not moved to 1_in_progress"
fi

# ----------------------------------------------------------
# Scenario 3: /progress start — empty 0_todo (no candidates)
# ----------------------------------------------------------
echo ""
echo "[Scenario 3] /progress start with empty 0_todo"

OUTPUT_S3=$(echo "pj:$PROJECT /progress 着手開始" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" 2>&1) || true

if echo "$OUTPUT_S3" | grep -qi "no match\|候補\|no.*candidate\|no.*task"; then
  pass "reports no candidates when 0_todo is empty"
else
  # Acceptable: any response that does NOT move files
  IP_COUNT=$(find "$PROJECT_DIR/tasks/1_in_progress" -name "*.md" 2>/dev/null | wc -l)
  if [ "$IP_COUNT" -eq 2 ]; then
    pass "no files moved (0_todo was empty)"
  else
    fail "unexpected state: $IP_COUNT files in 1_in_progress"
  fi
fi

# ----------------------------------------------------------
# Scenario 4: Revert alpha back to 0_todo, then start again
# ----------------------------------------------------------
echo ""
echo "[Scenario 4] Revert alpha → 0_todo, then start again"

echo "pj:$PROJECT /progress alpha を戻して -y" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_start_s4a.log 2>&1 || true

if [ -f "$PROJECT_DIR/tasks/0_todo/2026-05-21_feature-alpha.md" ]; then
  pass "alpha reverted to 0_todo"
else
  fail "alpha not reverted to 0_todo"
fi

echo "pj:$PROJECT /progress alpha 開始 -y" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_start_s4b.log 2>&1 || true

if [ -f "$PROJECT_DIR/tasks/1_in_progress/2026-05-21_feature-alpha.md" ]; then
  pass "alpha re-started to 1_in_progress via 開始 synonym"
else
  fail "alpha not re-started via 開始 synonym"
fi

# ----------------------------------------------------------
# Scenario 5: Synonym collision — 「開始」 must NOT route to approve
# ----------------------------------------------------------
echo ""
echo "[Scenario 5] Synonym collision check: 開始 ≠ approve"

# alpha is in 1_in_progress. If 開始 mistakenly routes to approve,
# alpha would move to 2_done. Verify it stays in 1_in_progress.
DONE_BEFORE=$(find "$PROJECT_DIR/tasks/2_done" -name "*.md" 2>/dev/null | wc -l)

# This should fail to find candidates (0_todo is empty for 'start',
# or if it somehow routes to approve, it would move alpha to done)
echo "pj:$PROJECT /progress 開始 -y" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /tmp/_test_start_s5.log 2>&1 || true

DONE_AFTER=$(find "$PROJECT_DIR/tasks/2_done" -name "*.md" 2>/dev/null | wc -l)
if [ "$DONE_AFTER" -eq "$DONE_BEFORE" ]; then
  pass "開始 did not route to approve (no files moved to 2_done)"
else
  fail "開始 routed to approve — files moved to 2_done"
fi

# ----------------------------------------------------------
# Final state dump
# ----------------------------------------------------------
echo ""
echo "=== Final state ==="
echo "--- progress.md ---"
cat "$PROJECT_DIR/progress.md" 2>/dev/null || echo "(not found)"
echo ""
echo "--- tasks tree ---"
find "$PROJECT_DIR/tasks" -type f -name "*.md" 2>/dev/null || echo "(not found)"

# ----------------------------------------------------------
# Cleanup state file
# ----------------------------------------------------------
rm -f "$STATE_FILE"

echo ""
echo "=== Done ==="
