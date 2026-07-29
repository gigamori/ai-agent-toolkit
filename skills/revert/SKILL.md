---
name: revert
description: |
  Undo recent assistant actions.
  Undo recent assistant actions.
  TRIGGER when: user says 戻す/戻せ/取り消す/undo/revert AND target is a recent assistant action (commit, file edit/write, git op) AND Claude Code
  SKIP: human-action revert; questions about revert semantics; Pi Coding Agent; Cursor
---

# revert

LLM の直近行動を **state-revert 原理** で安全に undo する skill。判定は専用 subagent に委ね、main agent は bias isolation のため判定・操作選定に関与しない。

## GATE — judge 介在は省略不可

本 skill が起動された時点で、main agent は **revert-judge subagent を必ず経由する**。これは GATE であり、条件付き手順ではない。

### 禁止事項（GATE 違反）

- main agent が自ら undo 対象を判断する
- main agent が自ら proposed_op を決定する
- main agent が user request の scope を解釈する（「全変更」「直前の」等の修飾語の意味判断を含む）
- Edit / Write / Bash を直接使って undo を試みる
- 「明らかなケースだから judge 不要」と判断する

上記はいずれも bias isolation 違反であり、事故 class B（rebase/reset --hard 等のショートカット選択）の直接原因となる。

### Required steps（省略・順序変更・代替いずれも不可）

1. `uv run scripts/revert_cc_log_extract.py --n 10` で `RECENT_LLM_ACTIONS` を取得
   - ユーザが「『xxx』と言った直前の状態に戻して」のように user message を基準に scope を指定した場合: `uv run scripts/revert_cc_log_extract.py --until-message "xxx"` を使用（`--n` は無視され、該当メッセージまでの全アクションを収集する）
2. `revert-judge` subagent に serialize protocol block を渡して判定 dispatch
3. JSON decision に **verbatim で** 従って分岐:
   - `execute` → G1〜G8 の実行手順に進む
   - `abstain` → 「undo 要求でない」と user 通知して終了
   - `ask_user` → user_question 提示 → user 回答後、**必ず step 1 から再実行**（cc_log_extract → revert-judge を再 dispatch、新しい USER_REQUEST に user の clarified intent を embed する）。「user 選択で確定した」を理由に G1 へ直行するのは **bias isolation 違反**
4. `execute` の場合のみ G1〜G8 に進む。**proposed_op は single shell command verbatim で実行**（cd / chain / wrapper 禁止）。G1 pre-flight・G5 verification はそれぞれ独立した Bash で実施

### なぜ GATE か

事故 04f3eba4（2026-05-19）: `/revert chat-panel-provider.ts の全変更を元に戻す` に対し、main agent が judge を経由せず `git restore`（セッション全体の 215 行を全消去）を提案。ユーザ意図は直前 1 ターンの 2 行のみ。judge の Step 2.5（ターンスコープ制約）が機能していれば防止できた事故。

## 核 insight: state revert 原理

「戻す／消す／delete／remove」の本質は **state revert**。Git の「消す」も同様。"make non-existent（全消去）" として処理すると射程超過。

**原理**: X recording を戻す。Y content は保持する。

```
時系列:  ──── Y ──── X ──── now
                       ↓ 「X を戻せ／消せ／delete X」
正:      ──── Y ──── ✕ ────       Y 保持、X 印のみ除去（戻す）
誤(B):   ──── ✕ ──── ✕ ────       Y も消した（make non-existent 誤読）
誤(C):   ──── Y ──── X ── ¬Y       Y を逆操作で打ち消し（やり過ぎ）
```

(B)(C) に該当する proposed_op は execute から ask_user に降格させる。

## 適用範囲

**LLM の直近行動を undo する文脈に限定**。人間作業の revert は明示指定で来るため推論不要。

## 発火語

### Group A（auto-route — hook が自動検出）

戻す／戻せ／戻して／取り消す／取り消せ／取り消して／undo／revert

これらは revert 文脈でのみ使われるため、UserPromptSubmit hook が検出し Skill('revert') を強制呼び出しする。

### Group B（`/revert` 明示呼び出し専用）

消す／消せ／削除／delete／remove／drop

これらは通常のコード編集指示でも頻出するため、hook では検出しない。revert 意図の場合はユーザが Group A の語を使うか、`/revert` で明示呼び出しする。revert-judge の abstain 判定では Group A+B 全語を参照する。

## Git 即答表

| ユーザ命令 | 一択 | 何が「戻る」か |
|---|---|---|
| commit を消せ | `git reset --soft <parent>` | HEAD ref が戻る、Y(diff) は保持 |
| 変更を消せ | `git restore <file>` | worktree が前状態に戻る |
| stage を消せ | `git restore --staged <file>` | index が前状態に戻る |
| ブランチを消せ | `git branch -D <branch>` | branch ref を除去（commit obj は GC まで保持） |
| ファイルを消せ | `git rm <file>` | worktree の entry を除去（history に保持） |

## Git 操作 → 影響 layer

| 操作 | 影響 |
|---|---|
| `reset --soft` | 記録 |
| `reset --mixed` | 記録 + 構成 |
| `reset --hard` | 記録 + 構成 + 内容 |
| `rebase` / `merge` | 記録 + 構成 + 内容 |
| `git revert` | 記録（追加 commit）+ 構成 + 内容 |
| `restore` | 内容（`--staged` で構成） |
| `checkout <ref>` | 構成 + 内容 |
| `checkout -- <file>` | 内容 |
| `branch -D` | 記録のみ |
| `cherry-pick` | 記録 + 構成 + 内容 |
| `stash apply/pop` | 構成 + 内容 |
| `filter-branch` / `filter-repo` | 記録 + 構成 + 内容（広範囲） |

## 表外の判定（5 秒で答えろ）

X は「記録／pointer／marker」か?

- **YES** → recording 層だけ戻す。下層 Y は保持
- **NO**（X = 内容そのもの） → X = Y。content ごと戻す（or 削除）
- **判別不能** → ユーザに確認

## 絶対禁止

- commit 消去で `rebase` / `reset --hard` / `git revert` を選ぶ → 図 (B)(C) に該当
- 「綺麗に／整合に」を理由に追加操作 → 拡張禁止
- 「消す = make non-existent」のまま全消去系を選ぶ → 素朴解釈禁止

## 実装方式

| 層 | 確定事項 |
|---|---|
| 名前 | `revert` |
| 形式 | Skill |
| 起動 | natural language auto-load + `/revert <args>` slash |
| 判定 | 専用 subagent `revert-judge`（fresh context、構造化 JSON 出力） |
| 客観事実 | `scripts/cc_log_extract.py`（session jsonl から直近 LLM アクション抽出） |
| abstain 通知 | user に skill load 通知してよい（silent 不要） |
| log 取得失敗 | `confidence: low` フラグ → ask_user 寄りに倒す |
| main agent | 判定にも操作選定にも触れない（bias isolation） |
| proposed_op が (B)(C) | execute → ask_user に降格 |

## Flow

1. revert skill auto-load（広め description）
2. `scripts/cc_log_extract.py` で直近 N アクション取得
3. `revert-judge` subagent に serialize protocol で渡す
4. JSON decision で分岐:
   - `execute`  → 「実行手順」に進む
   - `abstain`  → 「undo 要求でない」と user に通知して終了
   - `ask_user` → 質問提示 → 回答後 step 2 から再実行

## serialize protocol（subagent 入力）

```
USER_REQUEST: <verbatim>

RECENT_LLM_ACTIONS (newest first):
  --- Turn (latest, after user message at <ts>: '<preview>') ---
  1. [ts] Bash: git rebase --onto ...
  2. [ts] Write: package.json
  --- Turn (previous, after user message at <ts>: '<preview>') ---
  3. [ts] Edit: src/main.py
  ...

REPO_CONTEXT:
  type: git
  cwd: <path>
  current_branch: <name>

JUDGMENT_TASK: ユーザの要求は上記行動のいずれかを undo する要求か?
RESPONSE_FORMAT (JSON only):
  {
    "decision": "execute" | "abstain" | "ask_user",
    "target": {"action_idx": N, "summary": "..."} | null,
    "proposed_op": "<single shell command>" | null,
    "scope_check": "A" | "B" | "C" | null,
    "abstain_reason": "..." | null,
    "user_question": "..." | null
  }
```

`scope_check` が `B` または `C` の場合、subagent 自身が `decision: ask_user` に降格する。`A` のみ `execute` を許容する。

## 実行手順（decision: execute の場合）

`references/guidelines.md` の G1〜G8 を細則として参照しながら以下を順に行う。

1. **G1 Pre-flight checks**: repository state を確認（dirty worktree／merge in-progress／conflict／detached HEAD）。条件抵触時は ask_user に降格して user に状況提示
2. **G2 Preview**: proposed op／影響 layer／影響範囲／retrievability の 4 要素を 1 ブロックで提示
3. **G3 Confirmation**: 影響 layer に応じて分岐
   - 記録 layer 含む → `[y/N]` 確認必須、`y` のみで実行
   - 構成 / 内容 only → 通知のみ実行（reply 待たず）
4. **G4 Atomicity**: `proposed_op` を **single shell command** として実行する。複数 op 連鎖は禁止
5. **G5 Verification**: post-condition を最小限で検証（Git ref 改変系は `git status` + `git log -1 --oneline`、worktree 系は `git status`）
6. **G6 Reporting**: 何が戻ったか／何が保持されたか／retrievability hash／post-state snapshot を user に提示
7. **G7 Failure handling**: 非 0 exit code または verification 異常時は、`proposed_op` verbatim ・ exit code ・ stderr ・ `git status` ・ `git log -1` を dump して escalate。**retry / rollback / cleanup は禁止**
8. **G8 Logging**: revert action 自体の log は harness（cc-log）に委ねる。skill 側で追加 logging を実装しない

詳細仕様・Q1〜Q5 確定事項の根拠は [references/guidelines.md](references/guidelines.md) を参照。

## utility scripts

**scripts/revert_cc_log_extract.py**: session jsonl から直近 N アクションを抽出し、serialize protocol の `RECENT_LLM_ACTIONS` ブロックを生成する。

```bash
# 直近 N アクション（デフォルト）
uv run scripts/revert_cc_log_extract.py --n 10

# user message を基準にスコープ指定（--n 無視、該当メッセージまで全収集）
uv run scripts/revert_cc_log_extract.py --until-message "commitして"
```

`--until-message` 指定時は、該当文字列を含む user message が boundary となり、それ以降（新しい方）の全アクションが出力される。boundary message 自体は `--- Turn (target boundary, ...) ---` として表示される。

session jsonl の探索先は既定で `$CLAUDE_CONFIG_DIR/projects` → `~/.claude/projects` の順（設定されていれば両方を横断し、sid 衝突時は `$CLAUDE_CONFIG_DIR` 側が優先）。`--projects-dir` を明示した場合はその値のみを、従来どおりそのまま使う。

log 取得に失敗した場合は `confidence: low` フラグを subagent 入力に付加し、ask_user 寄りに判定を倒す。

## additional resources

- 実行ガイドライン本体（G1〜G8 細則、Q1〜Q5 確定事項）: [references/guidelines.md](references/guidelines.md)
- 判定 subagent 定義: [subagents/revert-judge.md](subagents/revert-judge.md)（master copy。`~/.claude/agents/revert-judge.md` への symlink で CC subagent registry に登録される）
