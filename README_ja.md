# ai-agent-toolkit

AIコーディングエージェント（Claude Code、Cursor など）向けの Claude Code プラグインと再利用可能なスキル集。

[English README](README.md)

## プラグイン

Claude Code プラグインマーケットプレイス経由で配布しています。

| プラグイン | 説明 |
|---|---|
| [taskflow](plugins/taskflow/) | 同時並行するタスクの進捗とコンテキストをセッション横断で管理 |
| [rule-inject](plugins/rule-inject/) | `CLAUDE.md` の `<rules when="..." src="..."/>` で宣言された外部ルールの読了を `PreToolUse` deny で強制。Claude Code と Cursor の両方で動作 |

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

両プラグインとも `.claude/` への symlink と `/…:init` による手動セットアップで Cursor でも動作する。詳細はプラグインごとの README を参照。

## スキル

プラグインなしで単体のエージェントに投入できる Agent Skill。

| スキル | 説明 |
|---|---|
| [create-skill](skills/create-skill/) | エージェントスキルの作成をガイド。ベストプラクティス、構造テンプレート、検証チェックリスト付き |
| [compact-document](skills/compact-document/) | マルチモードのドキュメント圧縮フレームワーク。記事、仕様書、議事録などを最小限の情報損失で凝縮 |

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
│   └── compact-document/      スキル: ドキュメント圧縮
├── LICENSE
├── README.md
└── README_ja.md
```

## コントリビューション

Issue や Pull Request を歓迎します。

## ライセンス

[MIT](LICENSE)
