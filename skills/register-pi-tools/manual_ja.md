# register-pi-tools — User Manual

このスキルが提供する **「Python スクリプトを Anthropic API tool として登録する仕組み」** のユーザ向けマニュアル。LLM 向けの仕様は同ディレクトリの `SKILL.md` 側を参照。

## 全体像

```
[ <input_dir>/*.py の各スクリプト ]
       ↓ frontmatter `args:` (JSON Schema) が単一情報源
       ↓
[ build_tools_yaml.py ]   ← 同ディレクトリ scripts/ 配下
       ↓ 全 .py を走査 → frontmatter を集約
       ↓
[ <output_path> = ~/.pi/agent/tools.yaml ]   ← レジストリ（build artifact、手書き禁止）
       ↓
[ ディスパッチャ（dispatch_tools.py / pi extension など）]
       ↓ load_tools → to_anthropic_tools → request.tools に乗せる
       ↓ tool_use 受信時は entry.command を spawn + stdin に JSON
[ Anthropic API ]
```

## ツールセットアップの 3 ステップ

### 1. スクリプトを書く / 既存スクリプトを移行する

Python スクリプトをいつも通り書く。引数取得部だけ次の規約に従う。

- frontmatter にスキーマを宣言（`args:` ブロック）
- `from _tool import args` を使う
- argparse は使わない

詳細仕様: 同ディレクトリの `SKILL.md` の "Per-script migration loop" を参照。

既存の argparse スクリプトを移行する場合は **このスキルを呼び出す** のが楽：

```
ユーザ「register-pi-tools で <ディレクトリ> を移行して」
LLM   → スキル auto-trigger → input_dir / output_path 確認 → 各 .py をマイグレーション
```

### 2. レジストリをビルドする

スクリプトが揃ったら、`build_tools_yaml.py` で `tools.yaml` を生成。

```bash
uv run --with pyyaml python ~/.claude/skills/register-pi-tools/scripts/build_tools_yaml.py \
  --input-dir <スクリプトのあるディレクトリ> \
  --output-path ~/.pi/agent/tools.yaml
```

引数:

| キー | 必須 | 既定値 | 意味 |
|---|---|---|---|
| `--input-dir` | ✓ | — | 走査対象ディレクトリ（再帰）。`_*.py` と `ignore-old/` は自動除外 |
| `--output-path` | — | `~/.pi/agent/tools.yaml` | 出力 yaml ファイルパス（チルダ展開対応） |

出力形式:

```yaml
- name: extract_frontmatter
  description: ファイルからfrontmatterを抽出する汎用ツール
  input_schema:
    type: object
    required: [file_or_dir]
    properties:
      file_or_dir: {type: string, description: ...}
      ...
  command: uv run python C:/home/doc/prompts/lib/src/extract_frontmatter.py
```

`tools.yaml` は **build artifact**。手で編集してはいけない。再ビルドで上書きされる。

### 3. ディスパッチャ経由で呼び出す

#### Python から

```python
import sys
sys.path.insert(0, "/path/to/lib/src")  # dispatch_tools.py のあるディレクトリ
from dispatch_tools import load_tools, to_anthropic_tools, dispatch

entries = load_tools("~/.pi/agent/tools.yaml")
tools_for_api = to_anthropic_tools(entries)  # request.tools に渡せる形

# tool_use 応答を受け取ったとき:
result = dispatch("extract_frontmatter", {"file_or_dir": "lib/src/_tool.py", "json": True}, entries)
print(result)  # str (UTF-8 stdout)
```

#### CLI 単独テスト（run_tool.py）

```bash
uv run python /path/to/lib/src/run_tool.py \
  --tool extract_frontmatter \
  --input '{"file_or_dir":"lib/src/_tool.py","json":true}'
```

引数:

| キー | 必須 | 既定値 | 意味 |
|---|---|---|---|
| `--tool` | ✓ | — | tools.yaml に登録された name |
| `--input` | — | `"{}"` | stdin に渡す JSON 文字列 |
| `--tools-yaml` | — | `lib/tools.yaml` | tools.yaml のパス（用途に応じて指定） |
| `--timeout` | — | なし | 実行タイムアウト秒数 |

#### pi 経由（将来）

`tools.yaml` を読み込んで `pi.registerTool({...})` する TypeScript 薄ラッパ extension で配布予定。pi-mono 本体への PR は不要（公開 extension API で完結する）。

#### Claude Code 互換性（直接は不可、MCP server 経由のみ）

`tools.yaml` を Claude Code に直接ロードすることは **できない**。Claude Code の tool レジストリは内部状態であり、slash command / skill / hook のいずれにも Anthropic API `request.tools` 配列にエントリを差し込む API は公開されていない。`UserPromptSubmit` hook はテキスト context の追加しかできず、構造化された tool 定義は注入できない。

唯一サポートされる経路は **MCP server**（Model Context Protocol）でラップする方法。`tools.yaml` の各エントリを MCP の `ListTools` / `CallTool` 経由で expose し、Claude Code（または任意の MCP 対応クライアント）の `settings.json` `mcpServers` で登録すれば自動的に tool として認識される。ディスパッチロジック（yaml ロード → `entry.command` を spawn → JSON を stdin に流す）は Python の `dispatch_tools.py` および計画中の pi extension と同一。Surface protocol だけが違う。pi extension とは別実装になるが、薄いコアライブラリを共有する設計は十分可能。

## ファイル一覧

| パス | 役割 |
|---|---|
| `~/.claude/skills/register-pi-tools/SKILL.md` | LLM 自動 trigger 用のスキル定義 |
| `~/.claude/skills/register-pi-tools/manual.md` | このファイル（人間向け） |
| `~/.claude/skills/register-pi-tools/scripts/build_tools_yaml.py` | レジストリビルド本体 |
| `~/.claude/skills/register-pi-tools/scripts/_tool.py` | runtime ヘルパ（同梱コピー） |
| `~/.pi/agent/tools.yaml` | 生成されるレジストリ（build artifact） |

ディスパッチャ系（`dispatch_tools.py` / `run_tool.py`）はスクリプト本体側のリポジトリに置く想定。本スキルは「frontmatter 移行＋レジストリ build」までを担当する。

## トラブルシュート

### `Error: $: missing required field 'XXX'`

スクリプトの frontmatter `args.required` で必須宣言したキーが渡されていない。stdin JSON に入れるか、argv `--xxx value` で渡す。

### `Error: $: unknown field 'YYY'`

stdin JSON または argv で渡したキーが frontmatter `args.properties` に無い。typo を確認。意図的に拡張キーを許す場合は schema に `additionalProperties: true` を追記。

### `invalid tool name 'foo bar' in /path/to/script.py`

frontmatter `name` が `^[a-zA-Z0-9_-]{1,64}$` を満たしていない（Anthropic API 制約）。空白・スラッシュ・ドットなどを除き、英数 + ハイフン + アンダースコアのみで命名する。

### `lost sys.stderr` / `I/O operation on closed file`

スクリプトが Windows stdout を `io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")` で再ラップしている。`_tool` 側でも reconfigure しているため二重ラップで shutdown 時に失敗する。`sys.stdout.reconfigure(encoding="utf-8")` に置き換えること。

### YAML パースエラー: `mapping values are not allowed here`

frontmatter の `description` 値に `:` や `{` `}` を含み、フロー記法で書いている。値を double quote で囲むか、block 記法に書き換える。

### `tools.yaml` が古い

`build_tools_yaml.py` を再実行する。手動編集はしない（次回ビルドで上書きされる）。

## 依存関係

- Python 3.10 以降
- `pyyaml`（uv の `--with pyyaml` で都度解決可。永続化するなら venv に `uv pip install pyyaml`）
- 各スクリプト固有の依存はそれぞれの frontmatter `usage` 行を参照

## 関連ドキュメント

- LLM 向けのスキル定義: 同ディレクトリの `SKILL.md`
- pi 本体の extension API 仕様: https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md
