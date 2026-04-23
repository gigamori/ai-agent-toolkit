# rule-inject

CLAUDE.md の `<rules when="..." src="..."/>` で宣言された外部ルールを、AI が対応ツールを呼ぶ前に **必ず Read させる** ことを強制する plugin。読み忘れを `PreToolUse` hook の `permissionDecision: "deny"` で決定論的に block し、AI の自己規律に頼らない。

Claude Code と Cursor の両方で動く。

[English README](README.md)

> 現在は PreToolUse タイミングのみをカバー。将来的に UserPromptSubmit 等の他タイミングへのルール注入も扱うため `rule-inject` という名称にしている。

## 何を解決するか

CLAUDE.md に「Python コマンド実行前に `lib/prompts/development/python_uv.md` を読め」と書いても、AI が読まずにコマンドを実行してしまうことがある。rule-inject は:

1. CLAUDE.md を解析し `<rules when="..." src="..."/>` を抽出
2. AI がツール（Bash / Write / Edit / MCP）を呼んだ瞬間、`when` 条件に合致するかチェック
3. 合致しかつ `src` を未読なら、該当ツール実行を block し「以下を Read してください」と返す
4. AI が Read したら pending list から除去され、次回 block されない

## インストール（Claude Code）

### マーケットプレイス経由（推奨）

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
```

### ローカル（開発・テスト用）

```bash
claude --plugin-dir ./plugins/rule-inject
```

## セットアップ

plugin を有効にした後、ワーキングディレクトリで初期化する:

```
/rule-inject:init
```

これにより:

- CWD 直近の `CLAUDE.md` を解析 → `hooks/when_detection.json` を生成
- CWD の MCP サーバ（`.cursor/mcp.json` / `.vscode/mcp.json` / `.claude.json` のいずれか）を検出 → 分類を質問
- `.claude/settings.json` に plugin 絶対パス展開済みの hook 設定を merge 書き込み

**再実行タイミング**:

- CWD の `CLAUDE.md` が変更された時
- MCP サーバが追加 / 削除された時
- plugin を別パスに再インストールした時（cache パスが変わる）

### Cursor で使う場合（手動セットアップ）

Cursor は Claude Code plugin 構造を認識しないが、`.claude/skills/` `.claude/settings.json` は自動読み込みする。以下を 1 度だけ実施:

#### 前提

- `uv` コマンドが PATH に通っていること
- Cursor の Third Party Hooks が有効であること
- Windows は管理者権限または開発者モード ON（symlink 作成のため）

#### 手順

1. **skill の symlink 作成**

   `<plugin>` は plugin 絶対パス（例: `C:\Users\<user>\.claude\plugins\rule-inject`）。ワークスペース CWD で:

   bash（WSL / Git Bash）:

   ```bash
   mkdir -p .claude/skills
   ln -s <plugin>/skills/init .claude/skills/init
   ```

   PowerShell（管理者）:

   ```powershell
   New-Item -ItemType Directory -Force .claude\skills | Out-Null
   New-Item -ItemType SymbolicLink -Path .claude\skills\init -Target <plugin>\skills\init
   ```

   cmd（管理者）:

   ```cmd
   mkdir .claude\skills 2>nul
   mklink /D .claude\skills\init <plugin>\skills\init
   ```

2. **フック設定書き出し**

   Cursor セッション内で:

   ```
   rule-inject を初期化して
   ```

   init skill が自動検出され、`.claude/settings.json` に plugin 絶対パス展開済みの hooks 設定が書き込まれる。Cursor の Third Party Hooks がこの `.claude/settings.json` を読む。

3. **確認**

   - `.claude/skills/init/SKILL.md` が symlink 先から参照可能
   - `.claude/settings.json` に `hooks.PreToolUse` と `hooks.PostToolUse` があり、`command` が plugin 絶対パスに展開されている

#### Cursor 側の既知の差異（Phase 0 検証で判明）

rule-inject plugin の hook スクリプトは下記の差異を吸収済み:

| 差異 | Cursor | Claude Code | 吸収方法 |
|---|---|---|---|
| hook stdin の encoding | UTF-8 BOM 付き | UTF-8 BOM 無し | スクリプトが `utf-8-sig` decode |
| Shell tool_name | `"Shell"` | `"Bash"` | `TOOL_NAME_ALIASES = {"Shell": "Bash"}` |
| hook_event_name | `"preToolUse"` (camelCase) | `"PreToolUse"` (PascalCase) | runtime 影響なし（matcher レイヤで auto-map される） |
| exit code 2 + stderr による deny | **非対応** | 対応 | hook は常に JSON `permissionDecision: "deny"` を出力する |

#### Cursor 側の制約

- plugin を別パスに再インストールしたら `rule-inject を初期化して` を再実行して `.claude/settings.json` の絶対パスを更新
- symlink 経由なので plugin 側 `SKILL.md` の更新は自動反映される
- Cursor ネイティブの `edit_file` / `delete_file` / `grep` 等のツール名は matcher に書かない（Claude 名 `Write` / `Edit` / `Grep` のみ使用。Cursor が auto-map する）

## 使い方

### CLAUDE.md での宣言

プロジェクトルートの `CLAUDE.md` に外部ルールを宣言する:

```xml
<rules priority="2" when="running python-related commands or executing python scripts" id="python_uv" src="lib/prompts/development/python_uv.md" />
<rules priority="2" when="performing git operations" id="git" src="lib/prompts/development/git.md" />
```

- `when`: 発火条件キーワード（自然言語可）
- `src`: 読了必須のファイルパス（workspace 相対）
- `id`: 任意（when 未指定時のフォールバック trigger）

`/rule-inject:init` はこの CLAUDE.md を解析し、`tool_signatures.json` の keyword-signature 対応表と突き合わせて `when_detection.json` を生成する。

### 発火する例

CLAUDE.md に `when="running python-related commands"` → `src="lib/prompts/development/python_uv.md"` がある状態で:

```
AI: uv run python script.py を実行します
→ PreToolUse hook 発火
→ python_uv.md 未読と判定
→ ツール実行 block
→ AI に "BLOCKED: 以下を Read してください: lib/prompts/development/python_uv.md"
AI: python_uv.md を Read
→ PostToolUse hook で pending list から削除
AI: 再度 uv run python script.py
→ pending list 空 → 通過
```

### 発火しない例

- `<rules>` タグに `when` も `id` も無い → 発火トリガーが無い
- `when` 文言に対応する signature が `tool_signatures.json` に無い → `/rule-inject:init` 実行時に WARN が出る
- ツールが `Bash` / `Write` / `Edit` / MCP 以外（`Read` 自体や `Grep`）→ matcher でフィルタされる

### pending を手動クリア

通常は Read で自動解除されるが、強制解除したい場合:

```bash
rm $TMPDIR/claude_rules_pending_<session_id>.txt
```

（`<session_id>` は AI が提示する reason 内に含まれている）

## 仕組み

### 全体フロー

```
AI: ツール実行（Bash / Write / Edit / MCP）
  │
  ├─ [PreToolUse hook: pre_tool_check_rules.py]
  │     ├─ stdin から session_id / tool_name / tool_input を取得（BOM 許容）
  │     ├─ tool_name alias (Shell→Bash) 適用
  │     ├─ CWD から上方向に CLAUDE.md を探索
  │     ├─ <rules when/src> を抽出
  │     ├─ when_detection.json と照合 → trigger/tool/field/pattern が一致する src を pending 追加
  │     ├─ 既読（reads log）の src は pending から除く
  │     └─ pending が空でなければ JSON permissionDecision=deny を出力
  │
  ├─ AI: 指示に従って該当 src を Read
  │
  ├─ [PostToolUse hook: post_track_reads.py]
  │     └─ Read 成功した file_path を reads log と pending から除去
  │
  └─ AI: 元のツール実行を再試行 → pending 空なら通過
```

### hook スクリプト

- `hooks/pre_tool_check_rules.py` — PreToolUse、deny 判定
- `hooks/post_track_reads.py` — PostToolUse、Read 追跡
- `hooks/generate_when_detection.py` — CLAUDE.md + tool_signatures.json → when_detection.json を生成（init skill が呼ぶ）
- `hooks/tool_signatures.json` — keyword → tool/field/pattern の対応辞書
- `hooks/when_detection.json` — init で生成される、プロジェクト固有の検出ルール

### state file

`tempfile.gettempdir()/claude_rules_pending_<session_id>.txt` — そのセッションで未読の src を保持  
`tempfile.gettempdir()/claude_reads_<session_id>.txt` — そのセッションで Read 済みの絶対パスを追記

session ごとに独立するので複数セッション並走で干渉しない。

## ロードマップ

| Phase | 内容 | 状態 |
|---|---|---|
| 0 | Cursor deny semantics 検証 | ✅ 完了（`permissionDecision: "deny"` honor 確認、stdin schema 差異特定） |
| 1 | plugin scaffold | ✅ 完了 |
| 2 | UTF-8 BOM 対応 + Shell→Bash alias | ✅ 完了 |
| 3 | init skill 実装 | ✅ 完了 |
| 4 | README 詳細化 | ✅ 本 README |

低優先 TODO:

- Cursor 上の `Edit` / MCP 呼び出しの tool_name 実値確認（Phase 0 C-2 で未観測のため）
- `tool_signatures.json` のキーワード拡充（現在は python / git / SQL 等の基本のみ）
- PreToolUse 以外のタイミング（UserPromptSubmit 等）へのルール注入対応

## ライセンス

[MIT](../../LICENSE)
