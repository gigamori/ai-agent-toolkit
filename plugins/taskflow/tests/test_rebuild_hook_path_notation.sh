#!/usr/bin/env bash
# test_rebuild_hook_path_notation.sh — verifies task_rebuild_progress.py resolves
# PostToolUse tool_input paths across absolute-path notations: Git-Bash/MSYS
# style (/c/...), native forward-slash (c:/...), and native backslash (c:\...).
# Regression for the silent-skip bug: /c/... resolved via os.path.isdir() under
# native Windows Python returns False (no drive letter), so the hook used to
# exit 0 without ever running rebuild_progress.py.
#
# Isolated: creates its own tempdir fixture, cd's into it, and never touches
# the repo's real _projects/ (task_rebuild_progress.py does not read/write
# _projects/_state/, but cwd isolation is kept for consistency with the
# project's E2E sandbox convention). Safe to re-run.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
HOOK="$REPO_ROOT/plugins/taskflow/hooks/task_rebuild_progress.py"

PASS=0; FAIL=0
ok(){ echo "  [o] $1"; PASS=$((PASS+1)); }
ng(){ echo "  [x] $1"; FAIL=$((FAIL+1)); }

TMP_RAW="$(mktemp -d)"
NATIVE_TMP="$(cygpath -m "$TMP_RAW")"   # e.g. C:/Users/.../AppData/Local/Temp/tmp.XXXX
NATIVE_TMP_BS="${NATIVE_TMP//\//\\}"    # backslash form
# This sandbox's mktemp -d resolves under /tmp (no drive letter), which is NOT
# the notation that caused the original bug (Git-Bash's /c/... drive mount —
# session f5e0e95b used /path/to/tree/pi/...). Re-derive the /c/...-style
# alias from NATIVE_TMP so Case 1/3 exercise the real regression, not a
# same-string no-op. Confirmed alias (same underlying dir) via cygpath -m.
DRIVE_LETTER=$(echo "${NATIVE_TMP:0:1}" | tr 'A-Z' 'a-z')
TMP="/$DRIVE_LETTER${NATIVE_TMP:2}"
echo "=== isolated tmpdir: $TMP (native: $NATIVE_TMP) ==="
cd "$TMP"

STATE_SNAPSHOT_BEFORE=$(find "$REPO_ROOT/_projects/_state" -type f 2>/dev/null | wc -l)

run_hook() { printf '%s' "$1" | uv run --no-project python "$HOOK" 2>/dev/null; }

setup_fixture() {
  local proj="$1"
  rm -rf "_projects/$proj"
  mkdir -p "_projects/$proj/tasks/0_todo" "_projects/$proj/tasks/1_in_progress" "_projects/$proj/tasks/2_done"
  printf '# Progress: %s\n\n<!-- @table:begin -->\n<!-- @table:end -->\n' "$proj" > "_projects/$proj/progress.md"
}

make_task() {
  local path="$1" title="$2"
  printf -- '---\npriority: MID\ncreated: 2026-07-24\nupdated: 2026-07-24\n---\n\n# %s\n\n<!-- @log:begin -->\n- 2026-07-24: created\n<!-- @log:end -->\n' "$title" > "$path"
}

# ----------------------------------------------------------
# Case 1 (repro): Bash command referencing Git-Bash /c/... style path
# ----------------------------------------------------------
echo ""
echo "--- Case 1 (repro): Bash mv with Git-Bash /c/... style path ---"
setup_fixture testpj1
make_task "_projects/testpj1/tasks/1_in_progress/2026-07-24_alpha.md" "Alpha One"
CMD1="mv $TMP/_projects/testpj1/tasks/0_todo/2026-07-24_alpha.md $TMP/_projects/testpj1/tasks/1_in_progress/2026-07-24_alpha.md"
OUT1=$(run_hook "{\"session_id\":\"case1\",\"tool_input\":{\"command\":\"$CMD1\"}}")
if echo "$OUT1" | grep -qi 'rebuilt\|unchanged'; then
  ok "Case1: /c/ 形式 mv でも hook が resolve して rebuild_progress.py を実行(silent skip しない)"
else
  ng "Case1: /c/ 形式 mv が resolve されない(silent skip)。OUT=$OUT1"
fi
if grep -q "Alpha One" "_projects/testpj1/progress.md"; then
  ok "Case1: progress.md に Alpha One が反映"
else
  ng "Case1: progress.md 未反映"
fi

# ----------------------------------------------------------
# Case 2 (regression, #45): Bash command with native c:/... style path
# ----------------------------------------------------------
echo ""
echo "--- Case 2 (regression): Bash mv with native c:/... style path ---"
setup_fixture testpj2
make_task "_projects/testpj2/tasks/1_in_progress/2026-07-24_beta.md" "Beta Two"
CMD2="mv $NATIVE_TMP/_projects/testpj2/tasks/0_todo/2026-07-24_beta.md $NATIVE_TMP/_projects/testpj2/tasks/1_in_progress/2026-07-24_beta.md"
OUT2=$(run_hook "{\"session_id\":\"case2\",\"tool_input\":{\"command\":\"$CMD2\"}}")
if grep -q "Beta Two" "_projects/testpj2/progress.md"; then
  ok "Case2: c:/ 形式 mv で progress.md に Beta Two が反映(既存動作の回帰なし)"
else
  ng "Case2: c:/ 形式 mv が退行。OUT=$OUT2"
fi

# ----------------------------------------------------------
# Case 3 (repro variant): Edit file_path with Git-Bash /c/... style path
# ----------------------------------------------------------
echo ""
echo "--- Case 3 (repro): Edit file_path with Git-Bash /c/... style path ---"
setup_fixture testpj3
make_task "_projects/testpj3/tasks/1_in_progress/2026-07-24_gamma.md" "Gamma Three"
FP3="$TMP/_projects/testpj3/tasks/1_in_progress/2026-07-24_gamma.md"
OUT3=$(run_hook "{\"session_id\":\"case3\",\"tool_input\":{\"file_path\":\"$FP3\"}}")
if grep -q "Gamma Three" "_projects/testpj3/progress.md"; then
  ok "Case3: /c/ 形式 Edit file_path でも progress.md に Gamma Three が反映"
else
  ng "Case3: /c/ 形式 Edit file_path が resolve されない。OUT=$OUT3"
fi

# ----------------------------------------------------------
# Case 4 (regression, #45): Edit file_path with native c:\... backslash style
# ----------------------------------------------------------
echo ""
echo "--- Case 4 (regression): Edit file_path with native c:\\... backslash style ---"
setup_fixture testpj4
make_task "_projects/testpj4/tasks/1_in_progress/2026-07-24_delta.md" "Delta Four"
FP4="${NATIVE_TMP_BS}\\_projects\\testpj4\\tasks\\1_in_progress\\2026-07-24_delta.md"
FP4_JSON="${FP4//\\/\\\\}"
OUT4=$(run_hook "{\"session_id\":\"case4\",\"tool_input\":{\"file_path\":\"$FP4_JSON\"}}")
if grep -q "Delta Four" "_projects/testpj4/progress.md"; then
  ok "Case4: c:\\ 形式 Edit file_path で progress.md に Delta Four が反映(既存動作の回帰なし)"
else
  ng "Case4: c:\\ 形式 Edit file_path が退行。OUT=$OUT4"
fi

# ----------------------------------------------------------
# Case 5: candidate extracted but unresolvable — must report, not silently skip
# ----------------------------------------------------------
echo ""
echo "--- Case 5: 存在しないプロジェクトパス参照 -> unresolved を報告(silent exit しない) ---"
FP5="$TMP/_projects/ghost-project-does-not-exist/tasks/1_in_progress/x.md"
OUT5=$(run_hook "{\"session_id\":\"case5\",\"tool_input\":{\"file_path\":\"$FP5\"}}")
if echo "$OUT5" | grep -qi 'unresolved'; then
  ok "Case5: 解決不能な候補を unresolved として additionalContext に報告"
else
  ng "Case5: 解決不能なのに無音 exit(旧挙動に後退)。OUT=$OUT5"
fi

# ----------------------------------------------------------
# Summary + isolation guarantee
# ----------------------------------------------------------
STATE_SNAPSHOT_AFTER=$(find "$REPO_ROOT/_projects/_state" -type f 2>/dev/null | wc -l)
if [ "$STATE_SNAPSHOT_BEFORE" = "$STATE_SNAPSHOT_AFTER" ]; then
  ok "isolation: 実 _projects/_state/ のファイル数は不変 ($STATE_SNAPSHOT_BEFORE)"
else
  ng "isolation: 実 _projects/_state/ のファイル数が変化 ($STATE_SNAPSHOT_BEFORE -> $STATE_SNAPSHOT_AFTER)"
fi

echo ""
echo "=== PASS=$PASS FAIL=$FAIL ==="
rm -rf "$TMP"
echo "tmpdir 削除済み: $TMP"

if [ "$FAIL" -gt 0 ]; then
  exit 1
fi
