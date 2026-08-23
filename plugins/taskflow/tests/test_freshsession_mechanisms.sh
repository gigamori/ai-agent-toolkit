#!/usr/bin/env bash
# Deterministic mechanism-level verification for the P2 AC-6 / P3-3 / P4 H1-3
# fresh-session checks. Runs the REAL session_init.py hook against an ISOLATED
# tempdir, plus static + tempdir-unit checks. Prints [o]/[x] per check. Safe to
# re-run. Does NOT touch the repo working tree.
#
# State-dir sandbox (`e2e_state_dir_sandbox` -- cited by rule id, not by path:
# the rule file has moved once already and every candidate path is gitignored,
# so no path citation survives a clone)
# --------------------------------------------------------------------------
# Since the 2026-08-20 ancestor-walk rollout, EVERY taskflow hook resolves its
# roots by walking UP from the cwd (cwd included) to the first directory that
# holds `_projects/_state`. So "cd into a tempdir" isolates nothing by itself:
# with no nearer `_projects/_state` on the walk, a temp dir sitting inside the
# repo tree resolves to the REAL one. That is not hypothetical -- the
# 2026-07-17 incident ran a regression with cwd at the repo root and deleted
# 250 real session-state files, in a directory that is gitignored and
# therefore unrecoverable.
#
# What isolates this script is:
#   (a) the step-4 guards immediately after mktemp below -- the temp dir
#       exists, is OUTSIDE the repo tree, and no ancestor of it holds
#       `_projects/_state`; and
#   (b) the fixture's own $TMP/_projects/_state, created before any hook runs,
#       which stops the walk at the cwd itself.
# (b) is ordering-fragile -- it holds only while every hook invocation happens
# after the fixture mkdir -- so (a) is the load-bearing half. Do NOT drop the
# guards on the argument that (b) suffices.
#
# The closing section asserts non-contact against the REAL `_projects/_state`:
# its file count before/after, and a lookup of each synthetic session id by its
# full 36 characters (a short prefix would collide with a live session and
# report a false leak). An earlier version of this script argued non-contact
# from the cwd argument alone -- "this script only does cd $TMP". The
# ancestor-walk rollout retired that argument, and an assertion that merely
# restates where the cwd was proves nothing about where the hooks wrote.
#
# Usage:  bash plugins/taskflow/tests/test_freshsession_mechanisms.sh
# Exit:   2 = sandbox-guard abort (nothing ran). Otherwise 0: the pass/fail
#         tally is reported on stdout as `PASS=<n> FAIL=<n>` and this script
#         has never encoded it in the exit status.
# Requires: bash (Git-Bash on win32 -- primary), uv.
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
# A failed mktemp would leave TMP empty, `cd ""` would fail silently under
# `set -uo pipefail` (no -e), the cwd would stay at $REPO_ROOT, and the guards
# below would pass an empty string -- the exact shape they exist to stop. Check
# before guarding, not after.
[ -n "$TMP" ] && [ -d "$TMP" ] \
  || { echo "ABORT: mktemp -d yielded no usable dir ('$TMP')" >&2; exit 2; }
cd "$TMP" || { echo "ABORT: cd '$TMP' failed" >&2; rm -rf "$TMP"; exit 2; }

# --- e2e_state_dir_sandbox step 4: abort BEFORE any fixture exists ----------
# Exit 2, not 1: these are not test failures -- nothing ran. The ancestor walk
# below includes $TMP itself, which is correct here precisely because the
# fixture's own _projects/_state does not exist yet.
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

# Synthetic session ids, declared up front so the closing leak check can look
# each one up by its full 36 characters.
SID_A="11111111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B="22222222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
PARENT="aaaaaaaa-1111-4111-8111-111111111111"
CHILD="bcbcbcbc-2222-4222-8222-222222222222"

echo ""
echo "--- P4 H1 (注入): session_init が pj: 有効時に [Progress Session]+index を注入 ---"
OUT1=$(run_si "{\"session_id\":\"$SID_A\",\"prompt\":\"pj:testpj こんにちは\"}")
if echo "$OUT1" | grep -q '\[Progress Session\]' \
   && echo "$OUT1" | grep -q 'current_project=testpj' \
   && echo "$OUT1" | grep -q '\[Project Index: testpj\]'; then
  ok "H1: pj:testpj → [Progress Session] + current_project=testpj + [Project Index: testpj] を注入"
else
  ng "H1: 期待した注入が無い。OUT=$OUT1"
fi

echo ""
echo "--- P2 AC-6 (機構): project未指定 → 注入抑制({}) = header非注入で router非誘発 ---"
OUT2=$(run_si "{\"session_id\":\"$SID_B\",\"prompt\":\"ただの質問です pj: なし\"}")
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
OUT1b=$(run_si "{\"session_id\":\"$SID_A\",\"prompt\":\"pj:testpj 続き\"}")
if echo "$OUT1b" | grep -q 'ROUTER: \[Progress Session\]' && echo "$OUT1b" | grep -q 'taskflow:project-router'; then
  ok "AC-6機構(有): turn2 の per-turn reminder に ROUTER: + taskflow:project-router を含む(=毎ターン再アンカー)"
else
  ng "AC-6機構(有): turn2 の注入に ROUTER cue が無い。OUT1b=$OUT1b"
fi

echo ""
echo "--- P4 H2 (fork継承): 親markerを持つtranscript+親state → project継承+inherited_tasks ---"
# 親/子 id は PARENT_MARKER_RE の厳密 hex UUID パターンに合わせる（[0-9a-f]のみ）
# 親 state（project=testpj）
printf '{"session_id":"%s","project":"testpj","rules_loaded":true,"guidelines_loaded":true,"indexed_project":"testpj"}' "$PARENT" \
  > "_projects/_state/${PARENT}.json"
# 親が着手中だったタスク（@log に親 sid8 を含む）
printf -- '---\npriority: MID\ncreated: 2026-07-18\nupdated: 2026-07-18\n---\n\n# 親タスク\n\n<!-- @log:begin -->\n- 2026-07-18T00:00:00+09:00 [s:%s]: parent work\n<!-- @log:end -->\n' "${PARENT:0:8}" \
  > "_projects/testpj/tasks/1_in_progress/2026-07-18_ptask.md"
# forked transcript（親 marker を含む jsonl）
printf '{"type":"user","message":{"role":"user","content":"[Progress Session] session_id=%s sid8=%s current_project=testpj"}}\n' "$PARENT" "${PARENT:0:8}" \
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
if uv run --no-project python "$CLEANUP_TEST_WIN" > "$TMP/cleanup.out" 2>&1; then
  LINE=$(grep -E 'All [0-9]+ checks passed' "$TMP/cleanup.out" || tail -1 "$TMP/cleanup.out")
  ok "H3/P4: test_cleanup_stale_markers 全PASS（非空state保持=self-heal/fork-memo不破壊を含む）: $LINE"
else
  ng "H3/P4: cleanup unit が失敗。末尾: $(tail -3 "$TMP/cleanup.out")"
fi

echo ""
echo "=== 実 _projects/_state/ 非接触の実測（cwd 引数ではなく実ディレクトリを見る）==="
# 1) 合成 SID の leak 検査。full 36 字で引く（短い prefix は生きたセッションに
#    当たって偽陽性になる）。
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
# 2) ファイル数の before/after。スイート内部の短い窓でのみ有効な指標であり、
#    生きたセッションの .touched churn で揺れうる（rules.md e2e_state_dir_sandbox
#    手順3）。上の full-id 照会が一次証拠、この件数は二次証拠。
REAL_STATE_AFTER=$(ls -1 "$REAL_STATE_DIR" 2>/dev/null | wc -l)
if [ "$REAL_STATE_BEFORE" = "$REAL_STATE_AFTER" ]; then
  ok "実 _projects/_state/ のファイル数が不変（$REAL_STATE_BEFORE -> $REAL_STATE_AFTER）"
else
  ng "実 _projects/_state/ のファイル数が変化（$REAL_STATE_BEFORE -> $REAL_STATE_AFTER）"
fi

echo ""
echo "=== 決定論チェック結果: PASS=$PASS FAIL=$FAIL ==="
cd "$REPO_ROOT"
rm -rf "$TMP"
echo "tmpdir 削除済み。"
