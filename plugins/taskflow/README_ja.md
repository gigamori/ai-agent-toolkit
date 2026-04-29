# taskflow

Claude Code プラグイン。同時並行するタスクの進捗とコンテキストを管理する。セッションをプロジェクトに紐付け、progress / handoff / project-notes による状態遷移とコンテキスト注入を提供する。

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

プラグインを有効にした後、ワーキングディレクトリで初期化する:

```
/taskflow:init myproject
```

これにより `_projects/` ディレクトリが作成され、`.claude/settings.json` に
hooks 設定が書き込まれる。

> **Claude Code 専用。** taskflow の毎ターン project routing は `UserPromptSubmit` の `additionalContext` 注入に依存している。Cursor の third-party 互換で auto-map される `beforeSubmitPrompt` は LLM コンテキスト注入を持たない（block 専用）ため、taskflow は Cursor 上では動作しない。背景は `_projects/harness-taskflow/project-notes/claude-plugin-to-cursor-compat.md` を参照。

## 使い方

### プロジェクト指定

プロンプト先頭に `pj:プロジェクト名` を付ける。省略時はLLMが推定する。

| 操作 | プロンプト例 |
|---|---|
| プロジェクト指定 | `pj:my-project ビルドエラーを直して` |
| プロジェクト指定 + コマンド | `pj:my-project /plan スキーマを設計せよ` |
| プロジェクト該当なし | `pj:none READMEを書いて` |
| 新規プロジェクト作成 | `新しいプロジェクト xxx を作って` |
| taskflow を完全バイパス（このターン） | `norouter READMEを書いて` |

### progress

タスクの状態管理。TODO / In Progress / Completed + Session Log。

| 操作 | プロンプト例 |
|---|---|
| 進捗確認 | `progressを見せて` |
| セッションログ記録 | `session logを書いて` |

TODO テーブルの `Prompt` 列にはタスク実行用のプロンプトがコピペ可能な形で記載されている。

### handoff

タスクに紐づく詳細コンテキスト。progress.md のinstruction（Prompt列）とセットで使う。

3つの状態を持つ:

| フォルダ | 状態 | 説明 |
|---|---|---|
| `0_pending/` | 未着手 | 新規作成先。セッション開始時に自動消費される |
| `1_in_progress/` | 作業中 | AIが消費済み。後続セッションで選択的に再読込される |
| `2_done/` | 完了 | 人間が承認して初めてここに移動する |

操作例:

| 操作 | プロンプト例 |
|---|---|
| handoff書き出し | `handoffを書いて` |
| 完了承認 | `このタスクを完了にして` / `handoff xxx.md を done にして` |
| 差し戻し | `このタスクを差し戻して` |
| 保留 | progress.md の In Progress 項目に `[HOLD]` を付与 |

progress.md のステータスを変更すると、対応するhandoffフォルダも連動して移動する。

### project-notes

プロジェクト固有の永続知識。AIが必要に応じて選択的に参照する。

| 操作 | プロンプト例 |
|---|---|
| 保存 | `この調査結果をnotesに保存して` |
| 一覧確認 | `notesに何がある？` |
| codebase記録 | `このリポの構造をnotesにまとめて` |

`project-notes/index.md` にファイル一覧が管理されている。notes作成・更新時に自動で更新される。

#### 調査系タスクの自動保存

ユーザーの意図が「情報収集・比較・整理・調査」である場合、project-router が意味ベースで検知し `project_notes_autosave: true` を返す。メインエージェントは応答本体を返したあと、ファイル名候補と共にユーザーに保存可否を確認する。承諾された場合のみ `project-notes/<slug>.md` と `project-notes/index.md` を更新する。拒否・無応答なら保存しない。

判定条件の詳細は `taskflow/prompts/project_router_agent.md` の `Step 2b`、保存フローの詳細は `taskflow/prompts/notes_guidelines.md` の「自動保存フロー」節を参照。

- 発火する例: 「このrepoの構造を調べて」「A案とB案を比較して」「handoffの運用を整理して」
- 発火しない例: 「READMEのtypo直して」「○○って何？」（単発説明要求）、「保存しないで」（明示拒否）

## ディレクトリ構造

```
_projects/
  index.md                    全プロジェクト一覧
  _state/                     セッション状態（自動管理）
  <project>/
    index.md                  プロジェクト概要
    progress.md               タスク進捗管理
    project-notes/
      index.md                project-notesインデックス
      *.md                    個別 project-notes
    handoff/
      0_pending/              未着手
      1_in_progress/          作業中
      2_done/                 完了（人間承認済み）
    plans/                    planコピー（自動・履歴保管）
    memory/                   memoryコピー（自動・履歴保管）
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
  │     必要 → progress.md / handoff / project-notes を読み書き
  │
  ├─ [LLM] project_notes_autosave判定 ─→ 調査系意図なら応答後に保存確認を提示
  │
  ├─ タスク実行
  │
  └─ [Stop hook] ─→ state_fileからプロジェクト名を読み、plan/memoryをコピー
```

### hook

2つのhookがプラグイン有効時に自動で動作する。

#### UserPromptSubmit: session_init.py

毎ターン実行。`_projects/_state/{session_id}.json` を管理し、`[Progress Session]` をLLMコンテキストに注入する。`_projects/` がCWDに存在しない場合は無害にスキップ。

#### Stop: session_sync.py

セッション終了時に実行。直近10分以内に更新された plan/memory ファイルをプロジェクトディレクトリにコピーする。
