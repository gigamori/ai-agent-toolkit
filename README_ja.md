# ai-agent-toolkit

AIコーディングエージェント（Claude Code、Cursor など）向けの Claude Code プラグインと再利用可能なスキル集。

[English README](README.md)

## プラグイン

Claude Code プラグインマーケットプレイス経由で配布しています。

| プラグイン | 互換性 | 説明 |
|---|---|---|
| [taskflow](plugins/taskflow/) | CC | 同時並行するタスクの進捗とコンテキストをセッション横断で管理 |
| [rule-inject](plugins/rule-inject/) | CC / Cursor | `CLAUDE.md` の `<rules when="..." src="..."/>` で宣言された外部ルールの読了を `PreToolUse` deny で強制 |

### インストール

このリポジトリをマーケットプレイスとして1度追加:

```
/plugin marketplace add gigamori/ai-agent-toolkit
```

必要なプラグインを個別にインストール:

```
/plugin install taskflow@ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
```

各プラグインの `README.md` にセットアップ手順、使い方、Cursor 互換情報が記載されている。

### Cursor で使う場合

**CC / Cursor** 表記のプラグイン・スキルは `.claude/` への symlink と `/…:init` による手動セットアップで Cursor でも動作する。**CC** 表記のものは Claude Code 固有機能（hooks, session JSONL, subagent）に依存するため Cursor では利用不可。詳細はプラグインごとの README を参照。

## スキル

プラグインなしで単体のエージェントに投入できる Agent Skill。

| スキル | 互換性 | 説明 |
|---|---|---|
| [create-skill](skills/create-skill/) | CC / Cursor | エージェントスキルの作成をガイド。ベストプラクティス、構造テンプレート、検証チェックリスト付き |
| [compact-document](skills/compact-document/) | CC / Cursor | マルチモードのドキュメント圧縮フレームワーク。記事、仕様書、議事録などを最小限の情報損失で凝縮 |
| [register-pi-tools](skills/register-pi-tools/) | CC / Cursor | Python スクリプトを YAML フロントマターの `args` (JSON Schema) と `_tool.args()` ランタイムに移行し、pi や Anthropic API ツール呼び出しから利用できる `tools.yaml` レジストリを生成 |
| [revert](skills/revert/) | CC | state-revert 原理に基づく安全な undo。判定を bias-isolated subagent に委任し、過剰除去を防止 |

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
│   └── rule-inject/             プラグイン: CLAUDE.md ルール強制
├── skills/
│   ├── create-skill/          スキル: スキル作成ガイド
│   ├── compact-document/      スキル: ドキュメント圧縮
│   ├── register-pi-tools/     スキル: Python スクリプト移行と tools.yaml 生成
│   └── revert/                スキル: bias-isolated 判定による安全な undo
├── LICENSE
├── README.md
└── README_ja.md
```

## コントリビューション

Issue や Pull Request を歓迎します。

## ライセンス

[MIT](LICENSE)
