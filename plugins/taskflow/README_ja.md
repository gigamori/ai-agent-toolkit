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
| プロジェクト該当なし | `pj:none READMEを書いて` |
| 新規プロジェクト作成 | `新しいプロジェクト xxx を作って` |
| taskflow を完全バイパス（このターン） | `norouter READMEを書いて` |

### progress

`progress.md` はタスクの index。手書き自由文セクション（Architecture / Key Decisions / Open Issues / Reference Materials）と、auto-generated テーブル領域（`<!-- @table:begin -->` ... `<!-- @table:end -->`、TODO / In Progress / Completed の各表）で構成される。

| 操作 | プロンプト例 |
|---|---|
| 進捗確認 | `progressを見せて` |
| テーブル再生 | `/progress rebuild` |
| drift 検出 | `/progress check` |

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
updated: 2026-05-13
---

# タスクタイトル（progress.md の row summary になる）

本文（mutable 領域 — 自由に置換可能）。

<!-- @log:begin -->
- 2026-05-13: 着手
- 2026-05-14: phase A 完了
<!-- @log:end -->
```

本文領域は自由編集、`<!-- @log -->` ブロックは append-only。

status 遷移:

| 操作 | プロンプト例 |
|---|---|
| タスク着手 | `/progress sync`（`mv` 後 or progress.md 編集後） |
| 完了承認 | `/progress approve <id>` |
| 差し戻し・再開 | `/progress revert <id>` |

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
  └─ [Stop hook] ─→ state_fileからプロジェクト名を読み、plan/memoryをコピー
```

### hook

2 つの hook がプラグイン有効時に自動で動作する。

#### UserPromptSubmit: session_init.py

毎ターン実行。`_projects/_state/{session_id}.json` を管理し、`[Progress Session]` を LLM コンテキストに注入する。`_projects/` が存在しない場合は自動生成する（`_state/` とテンプレート `index.md` も同時作成）。

#### Stop: session_sync.py

セッション終了時に実行。直近 10 分以内に更新された plan/memory ファイルをプロジェクトディレクトリにコピーする。
