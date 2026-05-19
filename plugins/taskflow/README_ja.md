# taskflow

Claude Code プラグイン。同時並行するタスクの進捗とコンテキストを管理する。セッションをプロジェクトに紐付け、`progress.md` / `tasks/` / `project-notes/` による状態遷移とコンテキスト注入を提供する。

[English README](README.md)

## インストール

### マーケットプレイス経由（推奨）

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install taskflow@ai-agent-toolkit
```

### ローカル（開発・テスト用）

```bash
claude --plugin-dir ./plugins/taskflow
```

## セットアップ

手動セットアップは不要。taskflow を有効化した workspace で最初のユーザプロンプトを送ると、`UserPromptSubmit` フックが `_projects/`、`_projects/_state/`、`_projects/index.md`（テンプレート）を自動生成する。

> **Claude Code 専用。** taskflow の毎ターン project routing は `UserPromptSubmit` の `additionalContext` 注入に依存している。Cursor の third-party 互換で auto-map される `beforeSubmitPrompt` は LLM コンテキスト注入を持たない（block 専用）ため、taskflow は Cursor 上では動作しない。背景は `_projects/harness-taskflow/project-notes/procedures/claude-plugin-to-cursor-compat.md` を参照。

## 使い方

### プロジェクト指定

プロンプト先頭に `pj:プロジェクト名` を付ける。省略時は LLM が推定する。

| 操作 | プロンプト例 |
|---|---|
| プロジェクト指定 | `pj:my-project ビルドエラーを直して` |
| プロジェクト指定 + コマンド | `pj:my-project /plan スキーマを設計せよ` |
| プロジェクト探索 | `pj:?` または `pj:? デプロイパイプライン` |
| プロジェクト該当なし | `pj:none READMEを書いて` |
| 新規プロジェクト作成 | `新しいプロジェクト xxx を作って` |
| taskflow を完全バイパス（このターン） | `norouter READMEを書いて` |

### /progress — タスク進捗コマンド

`/progress` はタスク状態の参照・変更の統一窓口。自然言語入力を `progress-router` サブエージェントが (action, targets) に解釈し、`AskUserQuestion` で確認（`-y` 指定時はスキップ）して実行する。

| sub-action | 効果 | 例 |
|---|---|---|
| `check` | drift / stale / 承認待ち検出。read-only。 | `/progress check` |
| `audit` | 各タスクを `## Next Steps` 状態で 4 区分（pending / completion candidate / untracked / clean）に分類。read-only。 | `/progress audit` |
| `rebuild`（`sync` は別名） | progress.md のテーブル領域を task ファイル群から再生成。 | `/progress rebuild` |
| `approve` | `1_in_progress/ → 2_done/` への移動。人間承認の遷移。 | `/progress approve 2026-05-14_xxx`<br>`/progress 完了 migration`<br>`/progress 全部完了 -y` |
| `revert` | 1 段戻す（`1_in_progress → 0_todo`、または `2_done → 1_in_progress`）。 | `/progress revert <prefix>`<br>`/progress 戻して audit` |

**action 同義語**（大文字小文字無視、入力に対する substring match）:

- approve: `approve`, `完了`, `終了`, `done`, `finish`, `ok`
- revert: `revert`, `戻す`, `戻し`, `undo`, `取り消し`
- `check` / `audit` / `sync` / `rebuild`: literal キーワードのみ

**target 解決**（優先度の高い match から採用）:

1. filename stem の先頭一致（大文字小文字無視）
2. filename stem の部分一致
3. H1 のセマンティック一致
4. plurality 表現（`全部` / `all` / `両方` / `両` 等）→ 全候補が match

**フラグ**:

- `-y` / `--yes` — 確認プロンプトをスキップして即実行

destructive アクション（`approve` / `revert`）では、メインエージェントが解決済 plan を表示し `AskUserQuestion` で確認を取ってから初めて mutation を実行する。`-y` で対象が確定済の場合はスキップ可。0 match / 低 confidence の複数 match の場合、router は候補を列挙して停止する（推測進行しない）。

### /dashboard — Kanban プロジェクトダッシュボード

`/dashboard` はすべての taskflow プロジェクトとそのタスクを kanban ボードで表示する自己完結型 HTML ダッシュボードを生成する。タスクはステータス（未着手・作業中・完了）とプロジェクト別に整理され、優先度バッジ、セッション履歴、セッションログ or `/progress` サブコマンドへのワンクリック遷移を提供。

ダッシュボードの特性:
- `_projects/index.md` からすべてのプロジェクトを読み込み、タスク一覧を列挙
- 各タスクの `@log` ブロックからセッション履歴を抽出し、short session ID を full UUID に解決（クリッカブルリンク化）
- 2 つのビューを備える（トグル切り替え）：**By Status**（ステータス別列）と **By Project**（プロジェクト別列）
- legend ボタンでプロジェクト・ステータスのリアルタイムフィルタリングを実装
- `/progress check`, `/progress audit`, `/progress rebuild` への quick-access dropdown を装備

起動方法：

| 方法 | コマンド | 結果 |
|---|---|---|
| skill 経由 | `/dashboard` | HTTP サーバーを `http://localhost:17329/` で起動、ブラウザ自動起動、Ctrl+C で停止 |
| script（静的） | `uv run python scripts/generate_dashboard.py` | HTML を `/tmp/taskflow-dashboard.html` に出力 |
| script（サーブ） | `uv run python scripts/generate_dashboard.py --serve --open` | サーバー起動＋ブラウザ自動起動 |

script のオプション:

- `--out PATH` — HTML 出力先を指定（デフォルト：`/tmp/taskflow-dashboard.html`）
- `--serve` — `localhost:17329` で HTTP サーバーを起動。`/open?session=<UUID>` と `/open?prompt=<...>` endpoints でセッション・プロンプト起動を実装
- `--open` — 生成後、デフォルトブラウザで自動起動
- `--scheme vscode|vscodium` — URI scheme を上書き（デフォルト：自動検出）

### progress.md

`progress.md` はタスクの index。手書き自由文セクション（Architecture / Key Decisions / Open Issues / Reference Materials）と、auto-generated テーブル領域（`<!-- @table:begin -->` ... `<!-- @table:end -->`、TODO / In Progress / Completed の各表）で構成される。テーブル再生は `/progress rebuild`、マーカー内側は手編集禁止。

### tasks

1 タスク 1 ファイル、`tasks/<status>/<date>_<topic>.md`。status はフォルダで表現する。

```
tasks/
  0_todo/             未着手
  1_in_progress/      作業中
  2_done/             完了（人間承認済）
```

task ファイル構造:

```markdown
---
priority: HIGH
created: 2026-05-13
updated: 2026-05-14
---

# タスクタイトル（progress.md の row summary になる）

本文（mutable 領域 — 自由に置換可能）。

## Next Steps
- 残作業項目 1
- 残作業項目 2

<!-- @log:begin -->
- 2026-05-13 [s:abc12345]: 着手
- 2026-05-14 [s:def67890]: phase A 完了 | next: テスト追加
<!-- @log:end -->
```

- 本文領域は自由編集、`<!-- @log -->` ブロックは **append-only**。
- `## Next Steps` が非空 = pending、`1_in_progress/` で空 = 完了候補。`Stop` フックがセッション中の実作業を見て LLM にこのセクションの更新を促す（[仕組み](#仕組み) 参照）。
- log 行には `[s:<session-id 先頭>]` タグが付き、audit の参照用 index になる。

status 遷移は `/progress approve` / `/progress revert` で行う（上記参照）。

#### v0.2.0 からの移行

`/progress audit` で `UNTRACKED` が検出された場合（`## Next Steps` セクション必須化以前の task）、repo ルートの移行スクリプトで一括 retrofit:

```bash
uv run python scripts/migrate_task_next_steps.py <project-root>
```

one-shot ツールで、plugin 配布対象外（repo 保守用のため plugin ではなく repo root の `scripts/` に配置）。

### project-notes

プロジェクト固有の永続知識、folder でカテゴリ分類:

| カテゴリ | 用途 |
|---|---|
| `specs/` | 仕様・設計・決定・ADR |
| `investigations/` | 調査・分析・post-mortem |
| `checks/` | 確認項目・チェックリスト（判定なし） |
| `procedures/` | 人間向け手順書 |
| `backlog/` | 候補・アイデア・issue |
| `_archive/` | 役目終了 |

| 操作 | プロンプト例 |
|---|---|
| 保存 | `この調査結果をnotesに保存して` |
| 一覧確認 | `notesに何がある？` |
| codebase 記録 | `このリポの構造をnotesにまとめて` |

`project-notes/index.md` は 4 列テーブル（`File | Description | Tags | Updated`）で notes を管理する。notes 作成・更新時に自動で更新される。

#### 調査系タスクの自動保存

ユーザーの意図が「情報収集・比較・整理・調査」である場合、project-router が意味ベースで検知し `project_notes_autosave: true` を返す。メインエージェントは応答本体を返したあと、カテゴリと slug の候補と共にユーザーに保存可否を確認する。承諾された場合のみ `project-notes/<category>/<slug>.md` と `project-notes/index.md` を更新する。

判定条件の詳細は `taskflow/prompts/project_router_agent.md` の `Step 2b`、保存フローの詳細は `taskflow/prompts/notes_guidelines.md` の「自動保存フロー」節を参照。

- 発火する例: 「このrepoの構造を調べて」「A案とB案を比較して」「○○の運用を整理して」
- 発火しない例: 「READMEのtypo直して」「○○って何？」（単発説明要求）、「保存しないで」（明示拒否）

## ディレクトリ構造

```
_projects/
  index.md                    全プロジェクト一覧
  _state/                     セッション状態（自動管理）
  <project>/
    index.md                  プロジェクト概要
    progress.md               タスク index
    tasks/
      0_todo/                 未着手
      1_in_progress/          作業中
      2_done/                 完了（人間承認済）
    project-notes/
      index.md                4 列 index
      specs/                  仕様・設計・決定
      investigations/         調査・分析・post-mortem
      checks/                 確認項目・チェックリスト
      procedures/             人間向け手順書
      backlog/                候補・アイデア
      _archive/               役目終了
    _archive/                 プロジェクトレベル archive
    plans/                    plan コピー（自動・履歴保管）
    memory/                   memory コピー（自動・履歴保管）
```

## 仕組み

### 全体フロー

```
セッション開始
  │
  ├─ [UserPromptSubmit hook] ─→ state_file作成 + pj:パース + session情報注入
  │
  ├─ [LLM] プロジェクト判定（常時実行）─→ state_fileにプロジェクト名を書き込み
  │
  ├─ [LLM] 適用判定 ─→ progress管理が必要か判断
  │     不要 → タスク実行のみ
  │     必要 → progress.md / tasks / project-notes を読み書き
  │
  ├─ [LLM] project_notes_autosave判定 ─→ 調査系意図なら応答後に保存確認を提示
  │
  ├─ タスク実行
  │
  └─ [Stop hooks] ─→ plan/memory コピーをアーカイブ、かつ
                     書込・編集系操作のあったセッションでは LLM に残 next step の記録を促す
```

### hook

3 つの hook がプラグイン有効時に自動で動作する。

#### UserPromptSubmit: session_init.py

毎ターン実行。`_projects/_state/{session_id}.json` を管理し、`[Progress Session]` を LLM コンテキストに注入する。`_projects/` が存在しない場合は自動生成する（`_state/` とテンプレート `index.md` も同時作成）。

ガイドライン注入も担当: セッション初回ターン（および compact 後）に `progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md` の全文を注入し、以降のターンではキーワードリマインダー（`guidelines_reminder.md`）のみを注入してトークンコストを削減する。

##### guidelines_reminder.md のメンテナンス

`prompts/guidelines_reminder.md` は初回以降の毎ターンに注入されるキーワードリマインダー。会話冒頭で注入された全文ガイドラインへの LLM のアテンションを再活性化する設計。

**設計原則**: 元のガイドラインから特徴的な用語（特に禁止規則、フォーマット固有パターン、権威の定義）を抽出し、対応する全文パッセージへのアテンション重みを強化する。

**メンテナンスルール**: 3 つのソースガイドライン（`progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md`）のいずれかを更新したら、`guidelines_reminder.md` も同じコミットで必ず更新すること。削除されたルールのキーワードが残ると幻覚的な制約を引き起こし、新ルールのキーワードが欠けるとサイレントな非準拠を招く。

**キーワード選定基準**（優先順）:

1. 禁止規則（してはいけないこと）— 忘却時の違反リスクが最も高い
2. フォーマット固有パターン（frontmatter フィールド、ファイル名規約、文字数制限）
3. 権威の定義（どのフィールドがどの情報源を正とするか）

#### Stop: session_sync.py

セッション終了時に実行。直近 10 分以内に更新された plan/memory ファイルをプロジェクトディレクトリにコピーする。

#### Stop: session_progress_capture.py

セッション終了時に `session_sync.py` と並列で実行。当該セッションの jsonl をスキャンし、write / edit / ファイル移動系ツール呼出があれば `{"decision":"block", "reason": ...}` を返し、touched files を埋め込んだ英語の imperative を注入する。LLM は各 task の `## Next Steps` を更新する（対応 task がなければ `0_todo/` か `1_in_progress/` に新規作成、完了したなら空にする）。state file の `progress_capture_done` フラグでセッションあたり 1 回に制限。touched files と `[s:<session-id 先頭>]` タグは実行時 substitution。設計は `_projects/harness-taskflow/project-notes/specs/progress-audit-design.md` を参照。
