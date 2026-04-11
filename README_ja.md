# ai-agent-toolkit

AIコーディングエージェント（Claude Code、Cursorなど）向けの再利用可能なスキル・フック・ハーネスコンポーネント集。

[English README](README.md)

## 概要

AIコーディングエージェントに専門的な機能を追加する、ポータブルなビルディングブロック集です。各コンポーネントは複数のエージェントプラットフォームで動作するよう設計されています。

## スキル一覧

| スキル | 説明 |
|--------|------|
| [create-skill](skills/create-skill/) | エージェントスキルの作成をガイド。ベストプラクティス、構造テンプレート、検証チェックリスト付き |
| [compact-document](skills/compact-document/) | マルチモードのドキュメント圧縮フレームワーク。記事、仕様書、議事録などを最小限の情報損失で凝縮 |

## インストール

スキルディレクトリをエージェントのスキルフォルダにコピーします:

```bash
# Claude Code
cp -r skills/create-skill ~/.claude/skills/

# Cursor
cp -r skills/create-skill ~/.cursor/skills/
```

リポジトリをクローンしてシンボリックリンクを作成する方法もあります:

```bash
git clone https://github.com/gigamori/ai-agent-toolkit.git
ln -s "$(pwd)/ai-agent-toolkit/skills/create-skill" ~/.claude/skills/create-skill
```

## ディレクトリ構成

```
ai-agent-toolkit/
├── skills/
│   ├── create-skill/       # スキル作成ガイド
│   └── compact-document/   # ドキュメント圧縮
├── LICENSE
├── README.md
└── README_ja.md
```

## コントリビューション

Issue や Pull Request を歓迎します。

## ライセンス

[MIT](LICENSE)
