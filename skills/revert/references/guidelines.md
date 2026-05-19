# revert skill 実行ガイドライン（G1〜G8）

## このドキュメントの位置づけ

`revert` skill の **execute 確定後の実行フェーズ** を規律する細則。判定（execute / abstain / ask_user）は `revert-judge` subagent が完了している前提で、proposed_op の実行を **安全性** と **観測可能性** で担保する。

判定ロジック・発火語・serialize protocol は `SKILL.md` を参照。

消費者: main agent（execute 経路の実行 step）。判定 subagent は本ファイルを参照しない。

---

## 設計判断（Q1〜Q5 確定）

### Q1 確認の granularity — B
layer 影響表（記録／構成／内容）で境界を引く。「変更を全部戻す」を毎回 y/n させるのは UX 重く、「commit を消す」「branch を消す」のような ref 改変は必ず確認。詳細は G3。

### Q2 protected branch policy — C-ii
`main` で直接作業するワークフローが現に存在するため、protected hardcode しない。警告も出さず Q1=B の通常確認に完全委任する。protected list の取得経路・default 値は不要。

### Q3 revert action の loggability — A
cc-log は harness が必ず記録する（追加実装不要）。bias isolation 原理から main agent は revert 判定に触れず、自動再帰経路は存在しない。再帰 revert は user の能動操作でしか発火しないため、深度制限は仕様化しない。

### Q4 failure 時 policy — A
observability over autonomy。proposed_op は serialize protocol で single shell command 固定（複数 op 連鎖は仕様外）。失敗時は exit code・stderr・現状の `git status` / `git log -1` を dump して終了し、user 判断に escalate。

### Q5 confirmation channel — B 精神（簡素化）
LLM は proposed_op と影響 layer / scope を提示し、user の y/n 相当 reply を待ち、`y` で実行に進む。UI primitive 抽象化 record（`{confirmation_required: ...}` 等）は不要。plain text 対話に委任。

---

## G1: Pre-flight checks

proposed_op 実行前に repository state を確認する。以下のいずれかに該当する場合、実行に進まず ask_user に降格して user に状況を提示する。

| 項目 | 確認方法 | 該当時の挙動 |
|---|---|---|
| dirty worktree | `git status --porcelain` が非空 | proposed_op が worktree を上書きする系（`reset --hard`、`checkout <ref>`、`restore`）の場合に限り ask_user。記録 layer のみの op（`reset --soft`、`branch -D`）は通常通り進める |
| in-progress merge | `.git/MERGE_HEAD` 存在 | ask_user。merge 解決を優先 |
| conflict 残存 | `git status` に `unmerged paths` | ask_user |
| detached HEAD | `git symbolic-ref -q HEAD` が失敗 | ask_user。branch ref 操作系で意図不明な場合がある |

ask_user に降格した場合、現状を verbatim で提示し、それでも実行を続けるか user に判断を委ねる。

## G2: Preview format

user 確認の直前に、以下の 4 要素を 1 ブロックで提示する。

1. **提案 op**: serialize protocol の `proposed_op` の verbatim
2. **影響 layer**: 記録 / 構成 / 内容 のいずれか（`SKILL.md` の Git 操作 → 影響 layer 表に従う）
3. **影響範囲**: 影響を受ける具体的対象（commit hash、branch 名、file 一覧）
4. **retrievability**: 実行後に元状態へ戻すための ref / hash（reflog 参照可否、`git reflog` 出力先頭の hash）

retrievability が無い op（例: `restore <file>` で worktree 変更を上書きする場合）は、その旨を明示する。

## G3: Confirmation

### 境界

| 影響 layer | 挙動 |
|---|---|
| 記録 を含む（`reset --soft/--mixed/--hard`、`branch -D`、`rebase`、`merge`、`cherry-pick`、`filter-branch` 等） | 確認必須（default y/n） |
| 構成 のみ（`restore --staged`） | 通知のみ実行 |
| 内容 のみ（`restore <file>`、`checkout -- <file>`） | 通知のみ実行 |

「通知のみ実行」は G2 preview を提示してそのまま実行に進む経路。reply を待たない。

### default y/n

確認必須 op では、G2 preview を提示した後 `[y/N]` を提示し、`y` の reply のみで実行する。`N`（default）またはその他 reply で cancel。

### bypass 条件

なし。Q5=B 精神に従い、自動 bypass は導入しない。

### 二重確認条件

なし。Q2=C-ii により、protected branch 等の特別な再確認も導入しない。

## G4: Atomicity

- proposed_op は single shell command 固定。serialize protocol の `proposed_op` フィールド以外を実行してはならない。
- 複数 op 連鎖は仕様外。連鎖が必要な要求は `revert-judge` 側で ask_user に降格させる対象。
- 失敗時の rollback は禁止（G7 参照）。
- cancel（user reply が `y` 以外）の場合、state を一切変更せず終了する。

## G5: Verification

proposed_op 実行直後に post-condition を最小限で検証し、結果を user に提示する。

| 操作系 | 検証コマンド |
|---|---|
| Git ref 改変系 | `git status` および `git log -1 --oneline` |
| Git worktree 改変系 | `git status` |
| 非 Git op | op に応じた最小確認。明示できない場合は省略可 |

検証で予期しない state が出た場合、追加操作はせず G7 に従って即時 escalate する。

## G6: Reporting

実行完了時に user へ以下を提示する。

1. **何が戻ったか**: `proposed_op` で除去された X recording の要約（HEAD / branch ref / worktree entry 等）
2. **何が保持されたか**: Y content の存続を明示（diff / file 内容 / commit object 等）
3. **retrievability**: G2 で記録した reflog hash を再掲。これにより user は元状態へ戻せる
4. **post-state snapshot**: G5 の検証コマンド出力

## G7: Failure handling

proposed_op が非 0 exit code を返した場合、または G5 verification が予期しない state を検出した場合：

1. 以下を **そのまま** dump する：
   - `proposed_op` の verbatim
   - exit code
   - stderr（あれば stdout も）
   - `git status` 出力
   - `git log -1 --oneline` 出力（Git op の場合）
2. retry / rollback / cleanup を **行わない**。これは scope_of_removal 違反として明示禁止。
3. user へ escalate して終了。後続の判断は user に委ねる。

## G8: Logging

- revert action 自体は harness（Claude Code 等）が cc-log（session jsonl）に記録する。skill 側で追加 logging を実装しない。
- 再帰 revert（直近の revert action を更に undo する要求）は user の能動的な再要求でのみ発火する。bias isolation 原理により main agent は判定に触れず、自動再帰経路は存在しない。
- 深度制限・再帰 flag は仕様化しない。
