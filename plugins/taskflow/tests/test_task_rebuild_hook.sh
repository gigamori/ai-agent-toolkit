#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/task_rebuild_progress.py"
PROJECTS_DIR="$REPO_ROOT/_projects"
PROJECT_NAME="_test-rebuild-hook-$$"
PROJECT_DIR="$PROJECTS_DIR/$PROJECT_NAME"

to_win() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
  else
    echo "$1"
  fi
}

FAKE_SESSION="test0000-0000-0000-0000-000000000000"

invoke_hook() {
  local win_path
  win_path=$(to_win "$1")
  local tool_name="${2:-Write}"
  echo "{\"session_id\":\"$FAKE_SESSION\",\"tool_name\":\"$tool_name\",\"tool_input\":{\"file_path\":\"$win_path\",\"content\":\"dummy\"}}" | uv run python "$(to_win "$HOOK")" 2>/dev/null || true
}

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $1"; }

cleanup() {
  rm -rf "$PROJECT_DIR"
  echo ""
  if [ "$FAIL" -eq 0 ]; then
    echo "All $PASS tests passed."
  else
    echo "$FAIL failed, $PASS passed."
  fi
}
trap cleanup EXIT

echo "=== Test: PostToolUse auto-rebuild hook ==="
echo "  project: $PROJECT_DIR"
  echo ""

mkdir -p "$PROJECT_DIR/tasks/0_todo"
mkdir -p "$PROJECT_DIR/tasks/1_in_progress"
mkdir -p "$PROJECT_DIR/tasks/2_done"

echo "[Scenario 1] Create first task in 0_todo"

TASK1="$PROJECT_DIR/tasks/0_todo/2026-05-19_test-feature-a.md"
cat > "$TASK1" << 'TASK'
---
priority: HIGH
created: 2026-05-19
updated: 2026-05-19
---

# Feature A の実装

テスト用タスク。

<!-- @log:begin -->
- 2026-05-19: created
<!-- @log:end -->
TASK

OUTPUT=$(invoke_hook "$TASK1")

if echo "$OUTPUT" | grep -q 'auto-rebuild'; then
  pass "hook returned auto-rebuild output"
  else
  fail "hook did not return auto-rebuild output: $OUTPUT"
  fi

if echo "$OUTPUT" | grep -q 'session=000000000000'; then
  pass "hook output includes session ID"
  else
  fail "hook output missing session ID: $OUTPUT"
  fi

if [ -f "$PROJECT_DIR/progress.md" ]; then
  pass "progress.md was created"
  else
  fail "progress.md was not created"
  fi

if grep -q "Feature A" "$PROJECT_DIR/progress.md"; then
  pass "task appears in progress.md TODO table"
  else
  fail "task not found in progress.md"
  fi

  echo ""
echo "[Scenario 2] Add second task in 0_todo"

TASK2="$PROJECT_DIR/tasks/0_todo/2026-05-19_test-feature-b.md"
cat > "$TASK2" << 'TASK'
---
priority: MID
created: 2026-05-19
updated: 2026-05-19
---

# Feature B の設計

テスト用タスク 2。

<!-- @log:begin -->
- 2026-05-19: created
<!-- @log:end -->
TASK

invoke_hook "$TASK2"

TODO_COUNT=$(grep -c "Feature" "$PROJECT_DIR/progress.md" || true)
if [ "$TODO_COUNT" -ge 2 ]; then
  pass "both tasks appear in progress.md ($TODO_COUNT matches)"
  else
  fail "expected 2+ Feature rows, got $TODO_COUNT"
  fi

  echo ""
echo "[Scenario 3] Move task to 1_in_progress and edit"

mv "$TASK1" "$PROJECT_DIR/tasks/1_in_progress/"
TASK1_MOVED="$PROJECT_DIR/tasks/1_in_progress/2026-05-19_test-feature-a.md"

invoke_hook "$TASK1_MOVED" "Edit"

if grep -q "In Progress" "$PROJECT_DIR/progress.md" && grep -q "Feature A" "$PROJECT_DIR/progress.md"; then
  pass "Feature A appears under In Progress"
  else
  fail "Feature A not found in In Progress section"
  fi

TODO_SECTION=$(sed -n '/^## TODO/,/^## In Progress/p' "$PROJECT_DIR/progress.md")
if echo "$TODO_SECTION" | grep -q "Feature A"; then
  fail "Feature A still in TODO after move"
  else
  pass "Feature A removed from TODO"
  fi

  echo ""
echo "[Scenario 4] Move task to 2_done"

mv "$TASK1_MOVED" "$PROJECT_DIR/tasks/2_done/"
TASK1_DONE="$PROJECT_DIR/tasks/2_done/2026-05-19_test-feature-a.md"

invoke_hook "$TASK1_DONE" "Edit"

COMPLETED_SECTION=$(sed -n '/^## Completed/,/<!-- @table:end -->/p' "$PROJECT_DIR/progress.md")
if echo "$COMPLETED_SECTION" | grep -q "Feature A"; then
  pass "Feature A appears in Completed"
  else
  fail "Feature A not found in Completed section"
  fi

  echo ""
echo "[Scenario 5] Non-task path is ignored"

OUTPUT_NONTASK=$(invoke_hook "/tmp/random-file.md")
if [ -z "$OUTPUT_NONTASK" ]; then
  pass "non-task path produces no output"
  else
  fail "non-task path produced output: $OUTPUT_NONTASK"
  fi

  echo ""
echo "[Scenario 6] Japanese slug in task filename"

TASK_JA="$PROJECT_DIR/tasks/0_todo/2026-05-19_日本語タスク.md"
cat > "$TASK_JA" << 'TASK'
---
priority: LOW
created: 2026-05-19
updated: 2026-05-19
---

# 日本語のタスク名テスト

マルチバイト文字のテスト。

<!-- @log:begin -->
- 2026-05-19: created
<!-- @log:end -->
TASK

invoke_hook "$TASK_JA"

if grep -q "日本語のタスク名テスト" "$PROJECT_DIR/progress.md"; then
  pass "Japanese task name appears in progress.md"
  else
  fail "Japanese task name not found in progress.md"
  fi

  echo ""
echo "=== Done ==="
