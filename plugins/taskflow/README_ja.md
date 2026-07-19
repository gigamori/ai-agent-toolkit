# taskflow

Claude Code プラグイン。同時並行するタスクの進捗とコンテキストを管理する。セッションをプロジェクトに紐付け、`progress.md` / `tasks/` / `project-notes/` による状態遷移とコンテキスト注入を提供する。

[English README](README.md)

> **taskflow は初めて？** まず [ユーザーガイド](USER_GUIDE_ja.md) から — 図つきのタスク指向な手引き。この README は機能リファレンス、[`docs/architecture.md`](docs/architecture.md) は内部設計。

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

手動セットアップは不要。workspace で初めて `pj:<project>` を使用したタイミングで、`UserPromptSubmit` フックが `_projects/`、`_projects/_state/`、`_projects/index.md`（テンプレート）を自動生成する。

> **Claude Code 専用。** taskflow の毎ターン project routing は `UserPromptSubmit` の `additionalContext` 注入に依存している。Cursor の third-party 互換で auto-map される `beforeSubmitPrompt` は LLM コンテキスト注入を持たない（block 専用）ため、taskflow は Cursor 上では動作しない。背景は `_projects/harness-taskflow/project-notes/procedures/claude-plugin-to-cursor-compat.md`（開発リポジトリの設計メモ。プラグインには同梱されない）を参照。

## 設定

### `TASKFLOW_PROJECT_ROOTS`

セミコロン区切りの `_projects/` ルートディレクトリ一覧。skill とスクリプトがこの変数を参照し、複数リポジトリにまたがるプロジェクトデータを検索する。

```bash
export TASKFLOW_PROJECT_ROOTS="/path/to/repo-a/_projects;/path/to/repo-b/_projects"
```

未設定の場合、現在の workspace の `_projects/` にフォールバックする。

Claude Code で恒久設定するには `settings.json` に追加:

```json
{
  "env": {
    "TASKFLOW_PROJECT_ROOTS": "/path/to/repo-a/_projects;/path/to/repo-b/_projects"
  }
}
```

### `TASKFLOW_GUIDELINES_REMINDER`

`session_init.py` が毎ターン注入する guidelines reminder の variant を選択する: `full`（デフォルト、~750 tok — 完全版キーワードリマインダー）または `manifest`（~460 tok — PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE の各ルールを recall label に圧縮したもの。毎ターン無条件発火が要件の ROUTER と RESPONSE LEADING LINES の2行はどちらの variant でも全文のまま）。未知の値・未設定は `full` にフォールバックする。

```json
{
  "env": {
    "TASKFLOW_GUIDELINES_REMINDER": "manifest"
  }
}
```

`manifest` は毎ターンコストを下げる代わりに、条件付きルール（PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE）のインライン可視性が下がるトレードオフを伴う — セッション開始時（および compact 後）に注入された全文ガイドラインへの依存度が上がる。

## 使い方

### プロジェクト指定

`pj:プロジェクト名` はプロンプトの冒頭（最初のほう）に置く。行頭または空白直後ならどこでも認識されるため、他の先頭行（`mode:` など）の後でもよく、物理的な先頭である必要はない。**`pj:` はメッセージの先頭 500 字以内に記述した場合のみ認識される。** 省略時は直前に設定済みのプロジェクトを維持する（文脈からの推定は行わない）。

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
| `start` | `0_todo/ → 1_in_progress/` への移動（完了タスクの再開 `2_done/ → 1_in_progress/` も担当）。 | `/progress start 2026-05-14_xxx`<br>`/progress 着手 migration` |
| `approve` | `1_in_progress/ → 2_done/` への移動。人間承認の遷移。（`0_todo/` からは 1 段飛ばしの jump として ⚠ 付きで確認。） | `/progress approve 2026-05-14_xxx`<br>`/progress 完了 migration`<br>`/progress 全部完了 -y` |
| `unstart` | タスクを未着手（TODO）へ移動（`1_in_progress → 0_todo`。`2_done` からは 1 段飛ばしの jump として ⚠ 付きで確認）。 | `/progress unstart <prefix>`<br>`/progress migration を未着手に` |

**state 目的語の同義語** — ユーザは到達させたい state を名指す（英語トークンは語境界一致 — 部分語ヒットなし・パス内は不算入、日本語トークンは substring 一致で重なりは最長優先、大文字小文字無視）:

- approve（`2_done`）: `完了`, `終了`, `done`, `finish`, `approve`
- start（`1_in_progress`）: `着手`, `開始`, `再開`, `進行中`, `start`, `begin`, `resume`
- unstart（`0_todo`）: `未着手`, `着手前`, `開始前`, `todo`, `unstart`
- `check` / `audit` / `sync` / `rebuild`: literal キーワードのみ
- undo/revert 語彙（`戻す` / `undo` / `revert` / `取り消し`）は意図的に不採用 — これらは LLM 行動の undo（例: グローバル revert skill）の領域であり、該当入力は `unknown` となり何も移動しない。

**target 解決**（優先度の高い match から採用）:

1. filename stem の先頭一致（大文字小文字無視）
2. filename stem の部分一致
3. H1 のセマンティック一致
4. plurality 表現（`全部` / `all` / `両方` / `両` 等）→ 全候補が match

**フラグ**:

- `-y` / `--yes` — 確認プロンプトをスキップして即実行

destructive アクション（`approve` / `start` / `unstart`）では、メインエージェントが解決済 plan を表示し `AskUserQuestion` で確認を取ってから初めて mutation を実行する。`-y` で対象が確定済の場合はスキップ可。0 match / 低 confidence の複数 match の場合、router は候補を列挙して停止する（推測進行しない）。

### /pj-rules — プロジェクト固有ルール

`/pj-rules` は、プロジェクトが任意で持つ `_projects/<project>/rules.md` — taskflow プロジェクト（`pj:`）単位でスコープされた規範（ファイルパス単位ではない）— を閲覧・編集する。router subagent は使わない（action 集合が小さく、write の本文はどのみち主エージェントが構成するため）。intent は小さな同義語表で inline 分類する。

| Action | 動作 | 例 |
|---|---|---|
| `show`（別名 `list`） | ルール本文・`## ` 見出し数・行数 vs cap を表示。read-only、確認不要。 | `/pj-rules show` |
| `write` | ルールの追加/編集、または `inject_every_turn`/`max_lines` frontmatter の変更。常に diff として提示し `AskUserQuestion` で承認後に適用。 | `/pj-rules add a rule: never edit dist/ directly` |

**`write` に `-y` スキップは無い。** `/progress` と異なり、この skill には確認バイパスが存在しない — `rules.md` はプロジェクトの以降の全ターンに注入されるため、blast radius が当該ターンを超えて及ぶ。入力中の `-y`/`--yes` は無条件に無視される。

承認済み write の後、`scripts/pj_rules.py show` を前後で実行して編集が `## ` 見出しを生成したか検証し（モデルの自己申告を信用しない）、続いて `scripts/pj_rules.py reset-indexed` で session state の `project_rules_indexed` フィールドのみをリセットする（merge-preserving — 他の state フィールドは無傷）。これにより次ターンで更新後の全文が再び表示される。

### /kanban — Kanban プロジェクトボード

`/kanban` はすべての taskflow プロジェクトとそのタスクを kanban ボードで表示する自己完結型 HTML を生成する。タスクはステータス（未着手・作業中・完了）とプロジェクト別に整理され、優先度バッジ、セッション履歴、セッションログ or `/progress` サブコマンドへのワンクリック遷移を提供。

kanban ボードの特性:
- `_projects/index.md` からすべてのプロジェクトを読み込み、タスク一覧を列挙
- 各タスクの `@log` ブロックからセッション履歴を抽出し、short session ID を full UUID に解決（クリッカブルリンク化）
- 2 つのビューを備える（トグル切り替え）：**By Status**（ステータス別列）と **By Project**（プロジェクト別列）
- legend ボタンでプロジェクト・ステータスのリアルタイムフィルタリング、および手動のライト / ダークテーマトグルを実装
- `/progress check`, `/progress audit`, `/progress rebuild` への quick-access dropdown を装備
- 各カードの **▶ CC** ボタンでタスクを Claude Code に起動（`pj:<project> @<タスクファイル>` をプリフィル）
- 各タスクの Markdown をブラウザ内ビューア（**📄**、serve モード）で表示：サニタイズ描画・クリッカブルなファイル参照（モーダル内遷移＋戻るボタン）・インライン画像
- 未参照セッションを可視化：プロジェクトに属すがタスク未紐付けの CC セッションを **No Task** 列／プロジェクト列内セクションに、どのプロジェクトにも属さない CC セッションを最右の **No Project** 列に表示（新しい順・上限あり）

起動方法：

| 方法 | コマンド | 結果 |
|---|---|---|
| skill 経由 | `/kanban` | サーバーをバックグラウンドでワークスペース由来の `http://localhost:<port>/` に起動（冪等 — このワークスペース向けに既に稼働中なら `already serving` を報告）し、URL と `--stop` コマンドを表示する（ブロックしない） |
| script（静的） | `uv run scripts/generate_kanban.py` | HTML を `/tmp/taskflow-kanban.html` に出力 |
| script（サーブ） | `uv run scripts/generate_kanban.py --serve --open` | サーバー起動＋ブラウザ自動起動 |

各ワークスペースのサーバーは、その `_projects` roots から導出したポート（base `17329`・span 64、プロセスを跨いでも `hashlib` により決定論的 — 同一ワークスペースからの後続の `--stop` が同じサーバーを発見できる）で待受する。複数の VSCode ワークスペースで同時に `/kanban` を実行してもポートが衝突しなくなった：各ワークスペースが個別のポートを持ち、`/health` にワークスペース識別キーを含めることで、ポートのハッシュ衝突が起きても別ワークスペースのサーバーを「既に稼働中」と誤認しない。

script のオプション:

- `--out PATH` — HTML 出力先を指定（デフォルト：システム一時ディレクトリ / `taskflow-kanban.html`）
- `--serve` — ワークスペース由来のポートで HTTP サーバーを起動。endpoints：`/open?session=<UUID>`・`/open?prompt=<...>`（セッション・プロンプト起動）、`/md?path=<file>`（サニタイズ済み Markdown 描画）、`/file?path=<file>`（プロジェクト配下の画像・添付配信）、`/health`
- `--stop` — このワークスペースの稼働中 `--serve` を停止（`/health` の pid 経由）
- `--stop --all` — 全ワークスペースの kanban サーバーをすべて停止
- `--port PORT` — 導出ポートの代わりに明示ポートを使用（`--serve`／`--stop` 両対応）
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
- `## Next Steps` が非空 = pending、`1_in_progress/` で空 = 完了候補。ガイドラインがエージェントにタスクを前進させたターンの終わりに `## Next Steps` を維持するよう指示し、`/progress audit` がコードで検査する（[仕組み](#仕組み) 参照）。
- log 行には `[s:<session-id 先頭>]` タグが付き、audit の参照用 index になる。
- task には、関連する `project-notes/` パスを列挙する自動管理ブロック `<!-- @notes:begin/end -->`（`@log:end` の直後に配置）が付くことがある。note↔task link 機構が書き込む（[仕組み](#仕組み) 参照）ため手編集禁止。

status 遷移は `/progress start` / `/progress approve` / `/progress unstart` で行う（上記参照）。

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

`project-notes/index.md` は 4 列テーブル（`File | Description | Tags | Updated`）で notes を管理する。notes 作成・更新時に LLM が（PreToolUse フックの促しを受けて）同期する。

project-router は project-notes を **pointer のみ**（ファイル一覧＋`project-notes/index.md` の該当行の逐語コピー、本文は返さない）で返すため、note 内容を要約・翻訳・confabulate して routing 結果に混入させない。本文が要るときはメインエージェントが直接読む。

#### 調査系タスクの自動保存

ユーザーの意図が「情報収集・比較・整理・調査」である場合、project-router が意味ベースで検知し `project_notes_autosave: true` を返す。メインエージェントは応答本体を返したあと、カテゴリと slug の候補と共にユーザーに保存可否を確認する。承諾された場合のみ `project-notes/<category>/<slug>.md` と `project-notes/index.md` を更新する。

判定条件の詳細は `taskflow/agents/project-router.md` の `Step 2b`、保存フローの詳細は `taskflow/prompts/notes_guidelines.md` の「自動保存フロー」節を参照。

- 発火する例: 「このrepoの構造を調べて」「A案とB案を比較して」「○○の運用を整理して」
- 発火しない例: 質問・確認（「○○って何？」「認証フローはどうなってる？」）、デバッグ・トラブルシュート、artifact 主体や単発些末な編集（「READMEのtypo直して」）、明示拒否（「保存しないで」）

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
  ├─ [SessionStart:compact hook]（auto-compaction 時のみ）─→ injection フラグをリセットし次ターンで guidelines を再注入
  │
  ├─ [UserPromptSubmit hook] ─→ state_file作成 + pj:パース + session情報 / guidelines 注入
  │
  ├─ [LLM] プロジェクト判定（プロジェクトが有効なときのみ。空のときはスキップ）─→ state_fileにプロジェクト名を書き込み
  │
  ├─ [LLM] 適用判定 ─→ progress管理が必要か判断
  │     不要 → タスク実行のみ
  │     必要 → progress.md / tasks / project-notes を読み書き
  │
  ├─ [LLM] project_notes_autosave判定 ─→ 調査系意図なら応答後に保存確認を提示
  │
  ├─ タスク実行
  │     ├─ [PreToolUse:Write|Edit] project-notes/ ファイル書込 ─→ project-notes/index.md 同期ルールを注入
  │     └─ [PostToolUse:Write|Edit|NotebookEdit|Bash] ─→ (a) tasks/ 変更時に progress.md テーブルを rebuild
  │                                                       (b) 書込先パスを per-session .touched ledger に追記
  │
  └─ [Stop hooks] ─→ plan/memory コピーをアーカイブ、かつ
                     当該セッションの作業を各 touched / owning task の @log に bind。
                     summary + note↔task-link の判定は async な progress-capture
                     サブエージェントに委譲する（不在時は決定論バックストップ）
```

### hook

7 つの hook スクリプトがプラグイン有効時に自動で動作する。`hooks/hooks.json` で `UserPromptSubmit` / `PreToolUse` / `PostToolUse`（2 つ）/ `Stop`（2 つ）/ `SessionStart:compact` に wire されている。加えて `note_links.py`（note↔task link のデータ層）と `log_lock.py`（task ごとの bounded advisory lock）の 2 ファイルは、Stop hook が import する共有モジュールであり、単体で wire された hook ではない。

#### UserPromptSubmit: session_init.py

毎ターン実行。`_projects/_state/{session_id}.json` を管理し、`[Progress Session]` を LLM コンテキストに注入する。`_projects/` が存在しない場合は自動生成する（`_state/` とテンプレート `index.md` も同時作成）。

ガイドライン注入も担当: セッション初回ターン（および compact 後）に `progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md` の全文を注入し、以降のターンでは reminder のみを注入してトークンコストを削減する — `guidelines_reminder.md`（デフォルト）または `guidelines_reminder_manifest.md`（`TASKFLOW_GUIDELINES_REMINDER=manifest`。[設定](#taskflow_guidelines_reminder) 参照）。

##### guidelines_reminder.md のメンテナンス

`prompts/guidelines_reminder.md` は初回以降の毎ターンに注入されるキーワードリマインダー。会話冒頭で注入された全文ガイドラインへの LLM のアテンションを再活性化する設計。`prompts/guidelines_reminder_manifest.md` は `TASKFLOW_GUIDELINES_REMINDER=manifest` で選択される低コスト variant: PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE の内容は recall label に圧縮されているが、毎ターン無条件発火が要件の ROUTER・RESPONSE LEADING LINES の2行は全文のまま残し、`guidelines_reminder.md` の同じ2行と byte 一致でなければならない（`tests/test_guidelines_reminder_mode.sh` が機械検証）。

**設計原則**: 元のガイドラインから特徴的な用語（特に禁止規則、フォーマット固有パターン、権威の定義）を抽出し、対応する全文パッセージへのアテンション重みを強化する。

**メンテナンスルール**: reminder に供給するソース — 3 つのガイドライン（`progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md`）＋ `project_routing.md`（ROUTER cue のソース）— のいずれかを更新したら、`guidelines_reminder.md` と `guidelines_reminder_manifest.md` の**両方**を同じコミットで必ず更新すること。削除されたルールのキーワードが残ると幻覚的な制約を引き起こし、新ルールのキーワードが欠けるとサイレントな非準拠を招く。

**キーワード選定基準**（優先順）:

1. 禁止規則（してはいけないこと）— 忘却時の違反リスクが最も高い
2. フォーマット固有パターン（frontmatter フィールド、ファイル名規約、文字数制限）
3. 権威の定義（どのフィールドがどの情報源を正とするか）

##### プロジェクトルール（rules.md）

`session_init.py` は、存在すればプロジェクト固有ルールファイル `_projects/<project>/rules.md` も注入する。ルールは taskflow プロジェクト（`pj:`）単位のスコープであり、ファイルパス単位ではない — パス/glob 単位のルールは `.claude/rules`、グローバルなルールは `CLAUDE.md` を使う。

- **プロジェクト切替時**: 全文を 1 回 *primer* として注入。
- **以降のターン**: ファイルの `##` 見出しの簡潔な manifest を recall cue（「行動前に読む」トリガー）として再掲し、全文を再注入せず低トークンコストでルールを温存する。
- **`inject_every_turn: true`**（ファイルの frontmatter）: 代わりに全文を毎ターン注入する — 常に温存されるが毎ターンのトークンコストがかかる。

ファイルは人間が編集する（モデル自己判断の書込なし）。エージェントは diff を提示し、ユーザ承認後にのみ適用する。state file の `project_rules_indexed` が切替時注入をゲートし、compaction 時にリセットされて primer が再注入される。設計の全文は `_projects/harness-taskflow/project-notes/specs/project-rules-injection.md`（開発リポジトリの設計メモ・配布物には含まない）を参照。

#### PreToolUse: notes_index_reminder.py (matcher: Write|Edit)

`_projects/<project>/project-notes/` 配下のファイル（`index.md` 自身は除く）への `Write`/`Edit` 直前に発火。`additionalContext` 経由で `[Project Notes Index Rule]` リマインダーを注入し、操作後に `project-notes/index.md` を同期するよう LLM に指示する（新規ファイルは行追加、Description/Tags 変更時は該当行更新、削除時は該当行除去）。

#### PostToolUse: task_rebuild_progress.py (matcher: Write|Edit)

`_projects/<project>/tasks/<status>/` 配下のファイルへの `Write`/`Edit` 直後に発火。`scripts/rebuild_progress.py` を実行して該当プロジェクトの `progress.md` テーブル領域を再生成し、手動の `/progress rebuild` なしでタスクインデックスを最新に保つ。

#### PostToolUse: touched_capture.py (matcher: Write|Edit|NotebookEdit|Bash)

すべての `Write` / `Edit` / `NotebookEdit` と、ファイルを触る `Bash`（`mv`/`cp`/`rm`、`>`/`>>` リダイレクト、`tee`）の直後に発火。書込先の正規化 repo-relative パスを per-session の `_projects/_state/{session_id}.touched` ledger（append-only、lock-free）に追記する。この ledger が、Stop の capture hook が「このセッションが実際に触った task」を判定する入力になる。jsonl スキャンや git diff ではなく **このセッションの tool 書込** を観測するため、無関係な task の誤 stamp を避けられる。サブエージェント / fork の内部書込は親の `session_id` で発火するため、自動的に親の ledger に入る。

#### SessionStart: session_compact_reset.py (matcher: compact)

Claude Code が会話を auto-compaction した際に発火。compaction では `session_init.py` が注入した `additionalContext` が失われるため、state file の injection フラグ（`rules_loaded`, `indexed_project`, `guidelines_loaded`, `project_rules_indexed`）をリセットする（他のフィールドは保持）。次の `UserPromptSubmit` ターンで `static_rules`・プロジェクトインデックス・全文ガイドライン・`rules.md` primer が再注入される。

#### Stop: session_sync.py

セッション終了時に実行。直近 10 分以内に更新された plan/memory ファイルをプロジェクトディレクトリにコピーする。

#### Stop: session_progress_capture.py

セッション終了時に `session_sync.py` と並列で実行。当該セッションの作業を、各 owning task の append-only `@log` ブロックに `- <ISO8601> [s:<sid>]: <summary>` 行として bind する。owning task の判定には `.touched` ledger（上記）と `[tasks:]` exec-binding carry（下記）を用いる。owner 判定 — touched task ごとの 1 行 summary と、新規書込された `project-notes/` 成果物の note↔task link — は async な `taskflow:progress-capture` サブエージェントに委譲する: hook は `capture.status=requested` を commit し、サブエージェント起動を促す block を 1 回返す。サブエージェントは `{session_id}.capture` JSON サイドカーを書き、後続の `Stop` がそれを決定論的に apply する（`@log` summary は `append_auto_binding`、note link は `append_note_link`）。15 秒の expiry 内にサイドカーが現れなければ、決定論バックストップが未 bind の touched task を placeholder-bind する。round / lifecycle 状態は `{session_id}.bind` サイドカーに保持する（state JSON とは分離し、他フックの並行書換による clobber を防ぐ）。旧 `{session_id}.captured` マーカーは legacy で、7 日クリーンアップで掃除されるのみ。設計は `_projects/harness-taskflow/project-notes/specs/exec-binding.md`（開発リポジトリの設計メモ。プラグインには同梱されない）と `project-notes/specs/note-task-link.md` を参照。

##### exec-binding（`[tasks:]` carry）

セッションの task 作業の結果が、その task 自身の `tasks/<status>/*.md` の **外** に落ちる場合（execution-by-reference — 例: task や handoff を読んで結果を別所に書く）、エージェントは応答の先頭行に `[tasks: a.md b.md]` で owning task のファイル名を列挙する。`session_progress_capture.py` はこの carry を読み、state の `exec_bind` 配列に union-merge し、各 owning task の `@log` に bind する。これにより `tasks/` を一切編集しなくても作業が記録される。task ファイルを直接編集した場合は `[tasks:]` は不要（PostToolUse の `.touched` capture が記録する）。

##### note↔task link（`@notes` block）

セッションが永続的な `project-notes/` 成果物を書くと、progress-capture サブエージェントがそれを owning task に対応付け、hook が link を **task 側** に記録する — task ファイル内の自動管理ブロック `<!-- @notes:begin/end -->` に project-relative な note パスを列挙する（`note_links.py`）。この link は、プロジェクトディレクトリの rename / move に task ファイルと共に追随する。stale なエントリ（ファイルが消えた note）は reverse index 構築時にスキップされる。

## 既知の問題

- **state file の競合**: 複数フック（`session_init.py`, `session_compact_reset.py`）が同一の `_projects/_state/{session_id}.json` をロックなしで読み書きする。実際にはトリガーイベント（`UserPromptSubmit` と `SessionStart:compact`）が同時発火しないためデータ損失は観測されていない。将来のリリースで atomic write または advisory lock の導入を予定。（capture の round 状態と touched ledger は、この種の clobber を避けるため `.bind` / `.touched` / `.capture` の別サイドカーに意図的に分離しており、`@log` / `@notes` 書込は `log_lock.py` の bounded advisory lock で直列化される。）
