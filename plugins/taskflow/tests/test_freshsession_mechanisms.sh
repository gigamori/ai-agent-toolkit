#!/usr/bin/env bash
# Deterministic mechanism-level verification for the P2 AC-6 / P3-3 / P4 H1-3
# fresh-session checks. Runs the REAL session_init.py hook against an ISOLATED
# tempdir (never the real _projects/_state/), plus static + tempdir-unit checks.
# Prints [o]/[x] per check. Safe to re-run. Does NOT touch the repo working tree.
set -uo pipefail

REPO="/path/to/tree/ai-agent-toolkit"
SI="$REPO/plugins/taskflow/hooks/session_init.py"
CAP_MD="$REPO/plugins/taskflow/agents/progress-capture.md"
REMINDER="$REPO/plugins/taskflow/prompts/guidelines_reminder.md"
CLEANUP_TEST="$REPO/plugins/taskflow/tests/test_cleanup_stale_markers.py"

PASS=0; FAIL=0
ok(){ echo "  [o] $1"; PASS=$((PASS+1)); }
ng(){ echo "  [x] $1"; FAIL=$((FAIL+1)); }

TMP="$(mktemp -d)"
echo "=== isolated tmpdir: $TMP ==="
cd "$TMP"
mkdir -p _projects/testpj/tasks/1_in_progress _projects/_state
printf '# testpj\n\nテスト用プロジェクト\n' > _projects/testpj/index.md
printf '# Progress: testpj\n\n<!-- @table:begin -->\n<!-- @table:end -->\n' > _projects/testpj/progress.md

run_si(){ echo "$1" | uv run --no-project python "$SI" 2>/dev/null; }

echo ""
echo "--- P4 H1 (注入): session_init が pj: 有効時に [Progress Session]+index を注入 ---"
OUT1=$(run_si '{"session_id":"11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa","prompt":"pj:testpj こんにちは"}')
if echo "$OUT1" | grep -q '\[Progress Session\]' \
   && echo "$OUT1" | grep -q 'current_project=testpj' \
   && echo "$OUT1" | grep -q '\[Project Index: testpj\]'; then
  ok "H1: pj:testpj → [Progress Session] + current_project=testpj + [Project Index: testpj] を注入"
else
  ng "H1: 期待した注入が無い。OUT=$OUT1"
fi

echo ""
echo "--- P2 AC-6 (機構): project未指定 → 注入抑制({}) = header非注入で router非誘発 ---"
OUT2=$(run_si '{"session_id":"22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb","prompt":"ただの質問です pj: なし"}')
OUT2C=$(echo "$OUT2" | tr -d '[:space:]')
if [ "$OUT2C" = "{}" ]; then
  ok "AC-6機構(空): project未指定 → 出力{} = [Progress Session] header 非注入 → router 非誘発"
else
  ng "AC-6機構(空): 未指定なのに注入された。OUT=$OUT2"
fi

echo ""
echo "--- P2 AC-6 (機構): pj有効時の per-turn reminder に ROUTER cue が含まれる（turn2以降）---"
# turn1(上のOUT1)は new session なので full guidelines が入る。ROUTER cue は
# per-turn reminder(guidelines_reminder.md)由来で turn2+ に注入される。
# 同一 session_id を2回叩いて turn2 の注入を検査する。
OUT1b=$(run_si '{"session_id":"11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa","prompt":"pj:testpj 続き"}')
if echo "$OUT1b" | grep -q 'ROUTER: \[Progress Session\]' && echo "$OUT1b" | grep -q 'taskflow:project-router'; then
  ok "AC-6機構(有): turn2 の per-turn reminder に ROUTER: + taskflow:project-router を含む(=毎ターン再アンカー)"
else
  ng "AC-6機構(有): turn2 の注入に ROUTER cue が無い。OUT1b=$OUT1b"
fi

echo ""
echo "--- P4 H2 (fork継承): 親markerを持つtranscript+親state → project継承+inherited_tasks ---"
# 親/子 id は PARENT_MARKER_RE の厳密 hex UUID パターンに合わせる（[0-9a-f]のみ）
PARENT="aaaaaaaa-1111-4111-8111-111111111111"
CHILD="bcbcbcbc-2222-4222-8222-222222222222"
# 親 state（project=testpj）
printf '{"session_id":"%s","project":"testpj","rules_loaded":true,"guidelines_loaded":true,"indexed_project":"testpj"}' "$PARENT" \
  > "_projects/_state/${PARENT}.json"
# 親が着手中だったタスク（@log に親 sid8 を含む）
printf -- '---\npriority: MID\ncreated: 2026-07-18\nupdated: 2026-07-18\n---\n\n# 親タスク\n\n<!-- @log:begin -->\n- 2026-07-18T00:00:00+09:00 [s:%s]: parent work\n<!-- @log:end -->\n' "${PARENT:0:8}" \
  > "_projects/testpj/tasks/1_in_progress/2026-07-18_ptask.md"
# forked transcript（親 marker を含む jsonl）
printf '{"type":"user","message":{"role":"user","content":"[Progress Session] session_id=%s sid8=%s current_project=testpj"}}\n' "$PARENT" "${PARENT:0:8}" \
  > "$TMP/fork.jsonl"
WIN_TR="$(cygpath -m "$TMP/fork.jsonl")"
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
echo "--- P3-3 (capture agent model): progress-capture.md が model: sonnet ---"
if grep -qE '^model:\s*sonnet\s*$' "$CAP_MD"; then
  ok "P3-3: agents/progress-capture.md frontmatter は model: sonnet（haiku から昇格済）"
else
  ng "P3-3: model が sonnet でない"
fi

echo ""
echo "--- P2 AC-6 (静的): guidelines_reminder.md に ROUTER cue 行が存在 ---"
if grep -q 'ROUTER: \[Progress Session\]' "$REMINDER" && grep -q 'taskflow:project-router' "$REMINDER"; then
  ok "AC-6静的: guidelines_reminder.md に per-turn ROUTER cue 行が存在"
else
  ng "AC-6静的: ROUTER cue 行が無い"
fi

echo ""
echo "--- P4 H3 (sweep安全性): _cleanup_stale_markers unit（tempdir隔離）が全PASS ---"
if uv run --no-project python "$CLEANUP_TEST" > "$TMP/cleanup.out" 2>&1; then
  LINE=$(grep -E 'All [0-9]+ checks passed' "$TMP/cleanup.out" || tail -1 "$TMP/cleanup.out")
  ok "H3/P4: test_cleanup_stale_markers 全PASS（非空state保持=self-heal/fork-memo不破壊を含む）: $LINE"
else
  ng "H3/P4: cleanup unit が失敗。末尾: $(tail -3 "$TMP/cleanup.out")"
fi

echo ""
echo "=== 決定論チェック結果: PASS=$PASS FAIL=$FAIL ==="
echo "=== 実 _projects/_state/ 未接触の確認（このスクリプトは cd $TMP のみで動作）==="
rm -rf "$TMP"
echo "tmpdir 削除済み。"
