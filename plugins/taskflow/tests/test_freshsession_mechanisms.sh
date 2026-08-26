#!/usr/bin/env bash
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
SI="$REPO_ROOT/plugins/taskflow/hooks/session_init.py"
CAP_MD="$REPO_ROOT/plugins/taskflow/agents/progress-capture.md"
REMINDER="$REPO_ROOT/plugins/taskflow/prompts/guidelines_reminder.md"
CLEANUP_TEST="$REPO_ROOT/plugins/taskflow/tests/test_cleanup_stale_markers.py"
REAL_STATE_DIR="$REPO_ROOT/_projects/_state"
REAL_STATE_BEFORE=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)

to_win() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else echo "$1"; fi; }
SI_WIN="$(to_win "$SI")"
CLEANUP_TEST_WIN="$(to_win "$CLEANUP_TEST")"

PASS=0; FAIL=0
ok(){ echo "  [o] $1"; PASS=$((PASS+1)); }
ng(){ echo "  [x] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)" || { echo "ABORT: mktemp -d failed" >&2; exit 2; }
[ -n "$TMP" ] && [ -d "$TMP" ] \
  || { echo "ABORT: mktemp -d yielded no usable dir ('$TMP')" >&2; exit 2; }
cd "$TMP" || { echo "ABORT: cd '$TMP' failed" >&2; rm -rf "$TMP"; exit 2; }

case "$TMP" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "ABORT: temp workspace $TMP is INSIDE the repo tree ($REPO_ROOT);" \
         "the hooks' ancestor walk would resolve into the real _projects/_state." >&2
    cd /; rm -rf "$TMP"; exit 2 ;;
esac
d="$TMP"
while :; do
  if [ -d "$d/_projects/_state" ]; then
    echo "ABORT: ancestor $d of temp workspace holds _projects/_state;" \
         "the hooks' ancestor walk would reach it." >&2
    cd /; rm -rf "$TMP"; exit 2
  fi
  p="$(dirname "$d")"; [ "$p" = "$d" ] && break; d="$p"
done

echo "=== isolated tmpdir: $TMP ==="
mkdir -p _projects/testpj/tasks/1_in_progress _projects/_state
printf '# testpj\n\nテスト用プロジェクト\n' > _projects/testpj/index.md
printf '# Progress: testpj\n\n<!-- @table:begin -->\n<!-- @table:end -->\n' > _projects/testpj/progress.md

run_si(){ echo "$1" | uv run --no-project python "$SI_WIN" 2>/dev/null; }

SID_A="11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B="22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PARENT="aaaaaaaa-1111-4111-8111-111111111111"
CHILD="bcbcbcbc-2222-4222-8222-222222222222"

echo ""
echo "--- injection: with pj: active, session_init injects [Progress Session] + index ---"
OUT1=$(run_si "{\"session_id\":\"$SID_A\",\"prompt\":\"pj:testpj こんにちは\"}")
if echo "$OUT1" | grep -q '\[Progress Session\]' \
   && echo "$OUT1" | grep -q 'current_project=testpj' \
   && echo "$OUT1" | grep -q '\[Project Index: testpj\]'; then
  ok "H1: pj:testpj → [Progress Session] + current_project=testpj + [Project Index: testpj] を注入"
else
  ng "H1: 期待した注入が無い。OUT=$OUT1"
  fi

echo ""
echo "--- no project assigned: injection suppressed ({}), so no header and no router cue ---"
OUT2=$(run_si "{\"session_id\":\"$SID_B\",\"prompt\":\"ただの質問です pj: なし\"}")
OUT2C=$(echo "$OUT2" | tr -d '[:space:]')
if [ "$OUT2C" = "{}" ]; then
  ok "AC-6機構(空): project未指定 → 出力{} = [Progress Session] header 非注入 → router 非誘発"
else
  ng "AC-6機構(空): 未指定なのに注入された。OUT=$OUT2"
  fi

echo ""
echo "--- with pj: active, the per-turn reminder carries the ROUTER cue (turn 2 onward) ---"
OUT1b=$(run_si "{\"session_id\":\"$SID_A\",\"prompt\":\"pj:testpj 続き\"}")
if echo "$OUT1b" | grep -q 'ROUTER: \[Progress Session\]' && echo "$OUT1b" | grep -q 'taskflow:project-router'; then
  ok "AC-6機構(有): turn2 の per-turn reminder に ROUTER: + taskflow:project-router を含む(=毎ターン再アンカー)"
else
  ng "AC-6機構(有): turn2 の注入に ROUTER cue が無い。OUT1b=$OUT1b"
  fi

echo ""
echo "--- fork inheritance: a transcript with the parent marker plus parent state yields the inherited project and inherited_tasks ---"
printf '{"session_id":"%s","project":"testpj","rules_loaded":true,"guidelines_loaded":true,"indexed_project":"testpj"}' "$PARENT" \
  > "_projects/_state/${PARENT}.json"
PARENT_CLEAN="${PARENT//-/}"
PARENT_TAG="${PARENT_CLEAN: -12}"
printf -- '---\npriority: MID\ncreated: 2026-07-18\nupdated: 2026-07-18\n---\n\n# 親タスク\n\n<!-- @log:begin -->\n- 2026-07-18T00:00:00+09:00 [s:%s]: parent work\n<!-- @log:end -->\n' "$PARENT_TAG" \
  > "_projects/testpj/tasks/1_in_progress/2026-07-18_ptask.md"
printf '{"type":"user","message":{"role":"user","content":"[Progress Session] session_id=%s sid=%s current_project=testpj"}}\n' "$PARENT" "$PARENT_TAG" \
  > "$TMP/fork.jsonl"
WIN_TR="$(to_win "$TMP/fork.jsonl")"
OUT3=$(run_si "{\"session_id\":\"$CHILD\",\"transcript_path\":\"$WIN_TR\",\"prompt\":\"続き\"}")
if echo "$OUT3" | grep -q 'current_project=testpj' \
   && echo "$OUT3" | grep -q "\[Forked Session\] parent_session=$PARENT"; then
  ok "H2: 子は親state から project=testpj を継承し [Forked Session] parent_session=$PARENT を注入"
  if echo "$OUT3" | grep -q 'inherited_tasks=.*2026-07-18_ptask.md'; then
    ok "H2: inherited_tasks に親の 1_in_progress タスク(2026-07-18_ptask.md)を収集"
else
    ng "H2: inherited_tasks が期待どおりでない。OUT=$OUT3"
  fi
else
  ng "H2: fork継承が働かない。OUT=$OUT3"
  fi

echo ""
echo "--- capture agent model: progress-capture.md declares model: sonnet ---"
if grep -qE '^model:\s*sonnet\s*$' "$CAP_MD"; then
  ok "P3-3: agents/progress-capture.md frontmatter は model: sonnet（haiku から昇格済）"
else
  ng "P3-3: model が sonnet でない"
  fi

echo ""
echo "--- static: guidelines_reminder.md carries the ROUTER cue line ---"
if grep -q 'ROUTER: \[Progress Session\]' "$REMINDER" && grep -q 'taskflow:project-router' "$REMINDER"; then
  ok "AC-6静的: guidelines_reminder.md に per-turn ROUTER cue 行が存在"
else
  ng "AC-6静的: ROUTER cue 行が無い"
  fi

echo ""
echo "--- sweep safety: the _cleanup_stale_markers unit checks (tempdir-isolated) all pass ---"
if uv run --no-project python "$CLEANUP_TEST_WIN" > "$TMP/cleanup.out" 2>&1; then
  LINE=$(grep -E 'All [0-9]+ checks passed' "$TMP/cleanup.out" || tail -1 "$TMP/cleanup.out")
  ok "H3/P4: test_cleanup_stale_markers 全PASS（非空state保持=self-heal/fork-memo不破壊を含む）: $LINE"
else
  ng "H3/P4: cleanup unit が失敗。末尾: $(tail -3 "$TMP/cleanup.out")"
  fi

echo ""
echo "=== measured non-contact with the real _projects/_state/ (reads the real directory, not the cwd argument) ==="
LEAKED=""
for s in "$SID_A" "$SID_B" "$PARENT" "$CHILD"; do
  for f in "$REAL_STATE_DIR/$s".*; do
    [ -e "$f" ] && LEAKED="$LEAKED $(basename "$f")"
done
done
if [ -z "$LEAKED" ]; then
  ok "実 _projects/_state/ に合成 session の成果物なし（4 SID を full 36 字で照会）"
else
  ng "実 _projects/_state/ に合成 session が leak した:$LEAKED"
  fi
REAL_STATE_AFTER=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
if [ "$REAL_STATE_BEFORE" = "$REAL_STATE_AFTER" ]; then
  ok "実 _projects/_state/ のファイル数が不変（$REAL_STATE_BEFORE -> $REAL_STATE_AFTER）"
else
  ng "実 _projects/_state/ のファイル数が変化（$REAL_STATE_BEFORE -> $REAL_STATE_AFTER）"
  fi

echo ""
echo "=== deterministic check result: PASS=$PASS FAIL=$FAIL ==="
cd "$REPO_ROOT"
rm -rf "$TMP"
echo "tmpdir removed."
