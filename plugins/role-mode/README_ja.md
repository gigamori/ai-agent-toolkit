# role-mode

ユーザーが各ターンで `mode:<name>` および/または `role:<value>` slug をプロンプトに含めると、framework meta（Two response axes / Mode > Role / answer-prefix 規約）+ 現在の Role/Mode 宣言 + 該当 mode ルール + 共通ルールが `UserPromptSubmit` hook で会話に注入される Claude Code プラグイン。どの slug も無いときは **何も注入されず**、プラグインを入れていない素の LLM 挙動と完全一致する。

**Claude Code 専用**。Cursor は **非対応**：Cursor の `beforeSubmitPrompt` hook は continue/block しか返せず context 注入機構を持たない。注入可能な hook は `sessionStart` のみで、これは会話開始時 1 回だけ発火するためターンごとの slug-based 注入と原理的に両立しない。

[English README is here](README.md)

## これが解決する問題

汎用的な LLM 応答は scope が曖昧になりがちで、同じプロンプトの中で「探索」「計画」「実行」が混ざることがある。role-mode はユーザーがターンごとに 1 つの認知フレームを選んで LLM をそこに縛る：

- `mode:survey` → 事実収集のみ、提案しない
- `mode:plan` → 手順を構造化、最終成果物は作らない
- `mode:execute` → 計画通りに実装、scope 拡張禁止
- `mode:debug` → broken 前提で原因究明
- ... 等

slug は opt-in / per-turn。slug 無しのときプラグインは不可視。

## インストール（Claude Code）

### plugin marketplace 経由（推奨）

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install role-mode@ai-agent-toolkit
```

### ローカル（開発・テスト）

```bash
claude --plugin-dir ./plugins/role-mode
```

別途 `init` ステップは不要。プラグインを有効化した時点で `UserPromptSubmit` hook が動作し、slug は自動検出される。

## 使い方

### Slug 構文

```
mode:<name>
role:<value>
role:"<value>"
```

`mode:` と `role:` は両方とも optional。少なくとも片方が存在すれば hook が発火。各 prefix とも入力中の **最初の出現** を採用、それ以降は無視。位置は自由（行頭 / 空白の直後）。taskflow の `pj:` と同方式。prefix のマッチは case-insensitive。

#### `mode:<name>`

- `<name>` は `[A-Za-z][A-Za-z0-9_-]*`。captured 値は lowercase 正規化
- 未知の mode 名は黙って無視（失敗・警告なし）
- mode alias：`verify` は `debug` に解決、`implement` は `execute` に解決。**ユーザーが選んだ alias は表示 `mode:` 行にそのまま保持**され、ルールファイル lookup のみ正規名を使う

#### `role:<value>`

- `<value>` は free-form（マルチバイト・スペース可）。verbatim 保持（case folding なし）
- 終端ルール 3 種、優先順：
  - **引用形** `role:"<value>"` → ダブルクォート内をそのまま捕捉。値内に literal `mode:` / `pj:` 等を含めたいときに使用
  - **非引用、次 slug** → ` mode:` または ` pj:` で停止
  - **非引用、行末 / EOF** → 改行または入力終端で停止
- 空 quote（`role:""`）は role なし扱い

例：

```
mode:plan auth リファクタの移行手順を設計して
pj:harness-modes mode:execute 設計ノート通りに scaffold を実装して
mode:debug role:厳格なコードレビュアー、セキュリティ重視で挙動を批判的に検証する
このコードを見て
role:"senior backend engineer" mode:debug この race condition を調査して
まずリポジトリ調査をして、その後 mode:survey で API 契約を一覧化して
```

### 利用可能な mode

| Mode | Aliases | こんなときに |
|---|---|---|
| `mode:ask` | | 単一質問への直接的・根拠付きの回答が欲しい |
| `mode:discuss` | | 専門家としての意見、結論に向けた議論が欲しい |
| `mode:brainstorm` | | 評価・根拠抜きで多様なアイデアが欲しい |
| `mode:organize` | | 散らかった考えを構造化したい |
| `mode:survey` | | 事実収集のみ、解決策・提案は不要 |
| `mode:plan` | | 明確な基準のあるアクション手順、最終成果物は不要 |
| `mode:execute` | `implement` | 計画を厳密に適用、scope 拡張禁止 |
| `mode:debug` | `verify` | 根本原因分析、早すぎる修正は禁止 |
| `mode:review` | | プロセス評価と教訓抽出 |

各 mode の `NEVER` / `DO` ルール全文は [`prompts/modes/`](prompts/modes/) を参照。

### 注入される内容

| 入力 slug | 注入されるブロック |
|---|---|
| `mode:` のみ | `_meta.md` + `mode: <name>` + mode rules + `_common.md` |
| `role:` のみ | `_meta.md` + `role: <value>` |
| 両方 | `_meta.md` + `role: <value>` + `mode: <name>` + mode rules + `_common.md` |
| どちらも無し | 何も注入しない（hook は silent exit） |

`_common.md`（mode 専用ルール）：

```markdown
## ALL MODES
- NEVER: overstep(mode boundary), change-mode-silently
- DO: declare(current mode), report(transition needs), cite(every claim except for brainstorming)
```

`_meta.md`（framework ヘッダ。任意の active slug と必ず一緒に注入される。`[BLOCKED: mode-rule <name>]` 自己宣言ルールと `[Mode: current_mode]` answer-prefix 指示を含む）。

### slug 無しのときの挙動

`mode:` / `role:` のどちらもプロンプトに無い場合、hook は何も出力せず exit する。LLM はプラグインを入れていないときと完全に同じプロンプトを受け取る。**設計上、挙動の変化はゼロ**。

## 仕組み

```
ユーザープロンプト
  │
  ├─ [UserPromptSubmit hook: mode_inject.py]
  │     ├─ stdin を読む（UTF-8 BOM 対応）
  │     ├─ MODE_RE: (?:^|\s)mode:([A-Za-z][A-Za-z0-9_-]*)         (case-insensitive, lowercase 正規化)
  │     ├─ ROLE_RE: (?:^|\s)role:(?:"([^"]*)"|(.+?)(?=...))       (case-insensitive prefix、値は verbatim)
  │     ├─ mode alias 解決（verify→debug, implement→execute）
  │     ├─ どの slug も無し → exit 0、無出力
  │     └─ それ以外 → JSON additionalContext = _meta.md + active block + (mode 設定時のみ mode rules + _common.md) を出力
  │
  └─ LLM はプロンプト＋注入された framework + active 宣言を受け取る
```

### ファイル構成

```
plugins/role-mode/
  .claude-plugin/plugin.json
  hooks/
    hooks.json
    mode_inject.py            # UserPromptSubmit hook
  prompts/modes/
    _meta.md                  # framework header（軸 / conflict / BLOCKED / answer prefix）
    _common.md                # ALL MODES ルール（mode 専用）
    ask.md
    discuss.md
    brainstorm.md
    organize.md
    survey.md
    plan.md
    execute.md
    debug.md
    review.md
```

各 `<mode>.md` は `Basic Behavior` / `NEVER` / `DO` の 3 行のみ。`mode: <name>` ヘッダは hook が動的生成し、ファイルには持たない。

## 他プラグインとの併用

### taskflow との併用

Claude Code 上で両プラグインとも `UserPromptSubmit` hook を使う。完全に独立で、taskflow はプロジェクト状態を、role-mode は mode ルールを注入する。Claude Code は hook 順序を保証しないが、2 つの出力は連結されるだけで合成されないので順序非依存。

`Plan` mode 定義は taskflow の `_projects/<project>/` scaffold 更新と意図的に共存可能：`NEVER: generate-final-deliverables` は design / process document の作成を許可しており、`progress.md` や `handoff/` 更新がこれに該当する。

### rule-inject との併用

rule-inject は `PreToolUse` / `PostToolUse`、role-mode は `UserPromptSubmit` で介入タイミングが異なるため衝突しない。両者とも `BLOCKED:` 自己宣言の規約を持つが、role-mode は `[BLOCKED: mode-rule <name>]` で識別子を分けている。

## ロードマップ

| Phase | 内容 | Status |
|---|---|---|
| 0 | mode ファイル + slug 検出 + 注入 hook（最小実装） | ✅ Done |
| 1 | README + marketplace 登録 | ✅ Done |
| 2 | mode 持続化（state-based `last_mode`） | 将来 |
| 3 | 違反検知 hook（PostResponse 系） | 将来 |

## ライセンス

[MIT](../../LICENSE)
