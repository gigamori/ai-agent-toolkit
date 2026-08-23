#!/usr/bin/env bash

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
PROJECT="_test-e2e-rebuild"
PROJECT_DIR="$REPO_ROOT/_projects/$PROJECT"
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
Follow tasks_guidelines.md conventions: task files go in tasks/0_todo/, 1_in_progress/, 2_done/ with frontmatter (priority, created, updated), H1 title, and log block. \
Filename pattern: YYYY-MM-DD_topic-slug.md. \
IMPORTANT: This is an automated test. Do NOT ask for confirmation. Do NOT scaffold index.md, project-notes, or progress.md. Just create/move/edit the task file directly. Do exactly what the user asks with no explanation."

echo "=== E2E Test: PostToolUse auto-rebuild via claude -p ==="
echo "  project dir: $PROJECT_DIR"
  echo ""

echo "[Scenario 1] Ask to create a task"

echo "pj:$PROJECT タスクを起票して: E2Eテスト用のダミー機能Alphaを実装する。priority HIGH。" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /dev/null 2>&1 || true

ALPHA_FILE=$(find "$PROJECT_DIR/tasks/0_todo" -name "*.md" 2>/dev/null | head -1)
if [ -n "$ALPHA_FILE" ]; then
  pass "task file created in 0_todo: $(basename "$ALPHA_FILE")"
  else
  fail "no task file found in 0_todo"
  fi

if [ -f "$PROJECT_DIR/progress.md" ]; then
  pass "progress.md auto-created by hook"
  else
  fail "progress.md not created (hook did not fire?)"
  fi

if [ -f "$PROJECT_DIR/progress.md" ] && grep -qi "alpha\|ダミー\|E2E" "$PROJECT_DIR/progress.md"; then
  pass "task appears in progress.md"
  else
  fail "task not found in progress.md"
  fi

  echo ""
echo "[Scenario 2] Ask to create a second task"

echo "pj:$PROJECT もう一つタスクを起票: Beta機能の設計。priority LOW。" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /dev/null 2>&1 || true

TODO_COUNT=$(find "$PROJECT_DIR/tasks/0_todo" -name "*.md" 2>/dev/null | wc -l)
if [ "$TODO_COUNT" -ge 2 ]; then
  pass "2 task files in 0_todo ($TODO_COUNT files)"
  else
  fail "expected 2+ files in 0_todo, got $TODO_COUNT"
  fi

  echo ""
echo "[Scenario 3] Ask to start work on Alpha task"

ALPHA_FILE=$(find "$PROJECT_DIR/tasks/0_todo" -name "*.md" 2>/dev/null | head -1)
ALPHA_BASENAME=$(basename "$ALPHA_FILE" 2>/dev/null)

echo "pj:$PROJECT タスク $ALPHA_BASENAME を着手（1_in_progress に移動して、ログに着手と記録）" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /dev/null 2>&1 || true

IP_COUNT=$(find "$PROJECT_DIR/tasks/1_in_progress" -name "*.md" 2>/dev/null | wc -l)
if [ "$IP_COUNT" -ge 1 ]; then
  pass "task moved to 1_in_progress ($IP_COUNT files)"
  else
  fail "no files in 1_in_progress"
  fi

if [ -f "$PROJECT_DIR/progress.md" ]; then
  IN_PROGRESS_SECTION=$(sed -n '/^## In Progress/,/^## Completed/p' "$PROJECT_DIR/progress.md")
  if [ -n "$IN_PROGRESS_SECTION" ] && echo "$IN_PROGRESS_SECTION" | grep -q "|"; then
    pass "In Progress table has entries in progress.md"
  else
    fail "In Progress table is empty in progress.md"
  fi
  fi

  echo ""
echo "[Scenario 4] Ask to complete the task"

IP_FILE=$(find "$PROJECT_DIR/tasks/1_in_progress" -name "*.md" 2>/dev/null | head -1)
IP_BASENAME=$(basename "$IP_FILE" 2>/dev/null)

echo "pj:$PROJECT タスク $IP_BASENAME を完了（2_done に移動して、ログに完了と記録）" \
  | $CLAUDE --system-prompt "$SYSTEM_PROMPT" > /dev/null 2>&1 || true

DONE_COUNT=$(find "$PROJECT_DIR/tasks/2_done" -name "*.md" 2>/dev/null | wc -l)
if [ "$DONE_COUNT" -ge 1 ]; then
  pass "task moved to 2_done ($DONE_COUNT files)"
  else
  fail "no files in 2_done"
  fi

if [ -f "$PROJECT_DIR/progress.md" ]; then
  COMPLETED_SECTION=$(sed -n '/^## Completed/,/<!-- @table:end -->/p' "$PROJECT_DIR/progress.md")
  if [ -n "$COMPLETED_SECTION" ] && echo "$COMPLETED_SECTION" | grep -q "|.*|"; then
    pass "Completed table has entries in progress.md"
  else
    fail "Completed table is empty in progress.md"
  fi
  fi

  echo ""
echo "=== Final state ==="
echo "--- progress.md ---"
cat "$PROJECT_DIR/progress.md" 2>/dev/null || echo "(not found)"
  echo ""
echo "--- tasks tree ---"
find "$PROJECT_DIR/tasks" -type f 2>/dev/null || echo "(not found)"

  echo ""
echo "=== Done ==="
