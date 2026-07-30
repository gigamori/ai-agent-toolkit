# ai-agent-toolkit

AIコーディングエージェント（Claude Code、Cursor など）向けの Claude Code プラグインと再利用可能なスキル集。

[English README](README.md)

## プラグイン

Claude Code プラグインマーケットプレイス経由で配布しています。

| プラグイン | 互換性 | 説明 |
|---|---|---|
| [taskflow](plugins/taskflow/) | CC | 同時並行するタスクの進捗とコンテキストをセッション横断で管理 |
| [rule-inject](plugins/rule-inject/) | CC / Cursor | `CLAUDE.md` の `<rules when="..." src="..."/>` で宣言された外部ルールの読了を `PreToolUse` deny で強制 |
| [role-mode](plugins/role-mode/) | CC | ターンごとに認知 `mode:` および/または `role:` を slug で宣言し、該当する NEVER/DO ルールと framework meta を `UserPromptSubmit` で注入（slug が無ければ何も注入しない） |
| [llm-wiki](plugins/llm-wiki/) | CC | LLM が維持管理する wiki：ソースを Markdown ページに ingest し、それらに接地して質問に答え、graph を lint/promote/view する。書込先 allowlist と単一 git トランザクション（失敗で rollback）の 2 code ゲートで保護 |

### インストール

このリポジトリをマーケットプレイスとして1度追加:

```
/plugin marketplace add gigamori/ai-agent-toolkit
```

必要なプラグインを個別にインストール:

```
/plugin install taskflow@ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
/plugin install llm-wiki@ai-agent-toolkit
```

各プラグインの `README.md` にセットアップ手順、使い方、Cursor 互換情報が記載されている。

### Cursor で使う場合

**CC / Cursor** 表記のプラグイン・スキルは `.claude/` への symlink と `/…:init` による手動セットアップで Cursor でも動作する。**CC** 表記のものは Claude Code 固有機能（hooks, session JSONL, subagent）に依存するため Cursor では利用不可。詳細はプラグインごとの README を参照。

## スキル

プラグインなしで単体のエージェントに投入できる Agent Skill。

`SKILL.md` に収まらない文書を持つスキルは、非 runtime のドキュメントを
[`docs/skills/`](docs/skills/index.md) 配下に置いている（ユーザガイド・authoring contract）。
一覧はそのインデックスに、スキルごとのリンクは下表の **Docs** 列にある。

| スキル | 互換性 | Docs | 説明 |
|---|---|---|---|
| [create-skill](skills/create-skill/) | CC / Cursor | — | エージェントスキルの作成をガイド。ベストプラクティス、構造テンプレート、検証チェックリスト、`validate_frontmatter.py` スクリプトに加え、高度なスキル設計・subagent プロトコル・パターン/実例・デバッグ・セキュリティの深掘りリファレンスを同梱 |
| [compact-document](skills/compact-document/) | CC / Cursor | [docs](docs/skills/compact-document/) | マルチモードのドキュメント圧縮フレームワーク（7 文書タイプ・モード自動判定）。記事、仕様書、議事録などを最小限の情報損失で凝縮。長文は chunk-brief（並列）→ merge → render の chunked map-reduce パイプラインで処理 |
| [compact](skills/compact/) | CC | — | `/compact <本文>` で呼び出す単発のドキュメント圧縮スキル。確認ゲートなしに 1 ターンで短縮・再構成・重複除去を行い、汎用の要約テンプレートに作り替えず元文書の章立てを継承する。`document_type`（8 種）と `compression_level`（light/standard/aggressive）を明示指定でき、数値・日付・modality・否定のスコープを改変しない rewrite fidelity を強制。`<source>` 内は命令ではなくデータとして扱う。組み込みの `/compact` コマンドを上書きする |
| [register-pi-tools](skills/register-pi-tools/) | CC / Cursor | [docs](docs/skills/register-pi-tools/) | Python スクリプトを YAML フロントマターの `args` (JSON Schema) と `_tool.args()` ランタイムに移行し、pi や Anthropic API ツール呼び出しから利用できる `tools.yaml` レジストリを生成。独立した EN/JA ユーザガイドと `build_tools_yaml.py` ビルダーを同梱 |
| [revert](skills/revert/) | CC | — | state-revert 原理に基づく安全な undo。判定を bias-isolated subagent に委任し、過剰除去を防止 |
| [debug-isolate](skills/debug-isolate/) | CC | — | 反復デバッグを forked subagent に隔離。git stash チェックポイントと連続失敗時の自動ロールバックで作業ツリーの状態を保全 |
| [run-sql](skills/run-sql/) | CC | — | 設定済みデータベース (PostgreSQL, MySQL, MariaDB, Redshift, Snowflake, BigQuery, DuckDB, Databricks) に対し SQL を実行し、生の JSON 結果を返す |
| [generate-debug-handoff](skills/generate-debug-handoff/) | CC | — | E2E テスト用の debug handoff Markdown を生成。`debugger:` 引数 (human/llm) で、LLM が整形補助に留まる（人間が承認）か debugger 役を担う（承認なし）かを選択 |
| [mode-orchestrator](skills/mode-orchestrator/) | CC | [docs](docs/skills/mode-orchestrator/) | todolist と context を含むドキュメントを読み、各ステップを role-mode の `mode:`/`role:` ヘッダ付きで隔離 `general-purpose` subagent ターンとして実行。1ターン 1 mode（+任意 role）、混在なし。autonomous mode 限定。ターンごとの model override、ターンごとに固定した status 行の返答契約（権限拒否や status 行の欠落はリカバリに入らず run を停止）、各ターンをウォールクロックで有界化し生成停止を検出する背景 watchdog、有界な `failed`→`debug`→再 execute リカバリループ、タスク種別ごとの workflow spec（`dev` 同梱）に対応 |
| [inspect-cc-log](skills/inspect-cc-log/) | CC | — | 過去の Claude Code セッションログを、事前構築した DuckDB ビュー（会話・引数付き tool 呼び出し・ファイル変更・fork・compaction・セッション集計）に対する SQL で調査。セッション再構成、tool/subagent 呼び出し監査、ファイル変更履歴の追跡、fork ツリーの束ね出力を、自己完結クエリスクリプト 1 本で実行 |
| [inspect-pi-log](skills/inspect-pi-log/) | CC | — | 過去の Pi Coding Agent セッションログを、事前構築した DuckDB ビュー（会話・tool 呼び出し・ファイル変更・セッション系譜/bundle・compaction・in-file branch・セッション集計）に対する SQL で調査。セッション再構成、tool/subagent 呼び出し監査、ファイル変更履歴の追跡、subagent/skill-fork/handoff/fork ツリーの束ね出力を、自己完結クエリスクリプト 1 本で実行 |
| [xml-wf](skills/xml-wf/) | CC | [docs](docs/skills/xml-wf/) | タスクを単一責務のステップに分解した XML v2 ワークフローを構築・実行・再開。同梱の Python ランナー（`wfrun`）が LLM ではなく決定論的にオーケストレーションする。バッチ実行は `--backend`（harness から自動判定）で選ぶ2つの backend を持つ: run-cc は各ステップを独立した `claude -p` subagent として実行し、run-pi は claude を一切必要とせず pi CLI で実行する（pi では強制できない `schema=` と `on-error="debug"` は起動前に拒否）。run-llm プロトコルはホスト側エージェント自身の subagent 機能へ委譲する |

### revert

AI アシスタントが不要な編集・commit・git 操作を行ったとき、「戻して」と言うだけで直前の変更を安全に undo できるスキル。アシスタントが過剰に巻き戻す事故（セッション全体の変更を消す等）を防止する。

1. **GATE 強制** — main agent が undo 対象や操作を自己判断することを禁止。全ての revert 要求は専用の `revert-judge` サブエージェント（fresh context で bias isolation）を必ず経由する。
2. **State-revert 原理** — 記録層（commit ref、branch pointer 等）のみを除去し、内容は保持する。内容も消す操作（scope B）や逆操作を追加する操作（scope C）は自動的にユーザ確認に昇格する。

**トリガー**: 会話中に `戻して` / `undo` / `revert` と発話、または `/revert <対象>` で明示呼び出し。

**ターンスコープ**: 「戻して」のような曖昧な要求は **直前 1 ターンのみ** を対象にする。セッション全体に暗黙で拡大することはなく、曖昧な場合は必ず確認する。

**必要環境**: Python 3.11+, [uv](https://docs.astral.sh/uv/), DuckDB（uv が自動インストール）。

スキルディレクトリをエージェントのスキルフォルダにコピー:

```bash
# Claude Code
cp -r skills/create-skill ~/.claude/skills/

# Cursor
cp -r skills/create-skill ~/.cursor/skills/
```

または clone してシンボリックリンク:

```bash
git clone https://github.com/gigamori/ai-agent-toolkit.git
ln -s "$(pwd)/ai-agent-toolkit/skills/create-skill" ~/.claude/skills/create-skill
```

## ディレクトリ構成

```
ai-agent-toolkit/
├── .claude-plugin/
│   └── marketplace.json       マーケットプレイスマニフェスト
├── plugins/
│   ├── taskflow/              プラグイン: タスク進捗 / コンテキスト管理
│   ├── rule-inject/           プラグイン: CLAUDE.md ルール強制
│   ├── role-mode/             プラグイン: ターンごとの認知 mode / role 注入
│   └── llm-wiki/              プラグイン: LLM が維持管理する wiki (ingest / query / lint / promote / view)
├── skills/
│   ├── create-skill/          スキル: スキル作成ガイド
│   ├── compact-document/      スキル: ドキュメント圧縮
│   ├── compact/               スキル: 単発のドキュメント圧縮 (/compact)
│   ├── register-pi-tools/     スキル: Python スクリプト移行と tools.yaml 生成
│   ├── revert/                スキル: bias-isolated 判定による安全な undo
│   ├── debug-isolate/         スキル: forked subagent による隔離デバッグ
│   ├── run-sql/               スキル: 設定済み DB への SQL 実行
│   ├── generate-debug-handoff/ スキル: E2E debug handoff Markdown 生成
│   ├── mode-orchestrator/      スキル: todolist を role-mode subagent ターンで実行
│   ├── inspect-cc-log/         スキル: CC ログ調査用の SQL ビュー群
│   ├── inspect-pi-log/         スキル: Pi Coding Agent ログ調査用の SQL ビュー群
│   └── xml-wf/                 スキル: 決定論的 XML v2 ワークフローランナー (wfrun)
├── docs/
│   └── skills/                非 runtime のスキル文書 (ユーザガイド・authoring contract)
│       ├── index.md           以下のスキル文書ディレクトリのインデックス
│       ├── xml-wf/            xml-wf リファレンス README + ユーザガイド (EN/JA)
│       ├── register-pi-tools/ register-pi-tools ユーザガイド (EN/JA)
│       ├── mode-orchestrator/ mode-orchestrator ユーザガイド (EN/JA) + workflow spec 執筆ガイド
│       └── compact-document/  compact-document authoring contract
├── LICENSE
├── README.md
└── README_ja.md
```

## ライセンス

[MIT](LICENSE)
