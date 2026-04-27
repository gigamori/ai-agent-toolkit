# role-mode

ユーザーが各ターンで `mode:<name>` slug をプロンプトに含めると、対応する **認知モード定義**（`NEVER` / `DO` ルールの最小セット）と全モード共通ルールが `UserPromptSubmit` hook で会話に注入される Claude Code プラグイン。slug が無いときは **何も注入されず**、プラグインを入れていない素の LLM 挙動と完全一致する。

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
```

- ユーザープロンプト内の **最初の出現** を採用、それ以降は無視
- 位置は自由（行頭 / 空白の直後）。taskflow の `pj:` と同方式
- 名前は小文字 ASCII（`[a-z][a-z0-9_-]*`）
- 未知の mode 名は黙って無視（失敗・警告なし）

例：

```
mode:plan auth リファクタの移行手順を設計して
pj:harness-modes mode:execute 設計ノート通りに scaffold を実装して
まずリポジトリ調査をして、その後 mode:survey で API 契約を一覧化して
```

### 利用可能な mode

| Mode | こんなときに |
|---|---|
| `mode:ask` | 単一質問への直接的・根拠付きの回答が欲しい |
| `mode:discuss` | 専門家としての意見、結論に向けた議論が欲しい |
| `mode:brainstorm` | 評価・根拠抜きで多様なアイデアが欲しい |
| `mode:organize` | 散らかった考えを構造化したい |
| `mode:survey` | 事実収集のみ、解決策・提案は不要 |
| `mode:plan` | 明確な基準のあるアクション手順、最終成果物は不要 |
| `mode:execute` | 計画を厳密に適用、scope 拡張禁止 |
| `mode:debug` | 根本原因分析、早すぎる修正は禁止 |
| `mode:review` | プロセス評価と教訓抽出 |

各 mode の `NEVER` / `DO` ルール全文は [`prompts/modes/`](prompts/modes/) を参照。

### 共通ルール（任意の mode と一緒に必ず注入）

```markdown
## ALL MODES
- NEVER: overstep(mode boundary), change-mode-silently
- DO: declare(current mode), report(transition needs), cite(every claim except for brainstorming)

On rule violation, stop and self-report with the marker `[BLOCKED: mode-rule <name>]` before proceeding.
```

### slug 無しのときの挙動

`mode:<name>` がプロンプトに無い場合、hook は何も出力せず exit する。LLM はプラグインを入れていないときと完全に同じプロンプトを受け取る。**設計上、挙動の変化はゼロ**。

## 仕組み

```
ユーザープロンプト
  │
  ├─ [UserPromptSubmit hook: mode_inject.py]
  │     ├─ stdin を読む（UTF-8 BOM 対応）
  │     ├─ regex 検索: (?:^|\s)mode:([a-z][a-z0-9_-]*)
  │     ├─ 不一致 → exit 0、無出力
  │     ├─ 一致するが対応ファイルが無い → exit 0、無出力
  │     └─ それ以外 → JSON additionalContext = mode ファイル + _common.md を出力
  │
  └─ LLM はプロンプト＋注入された mode ルールを受け取る
```

### ファイル構成

```
plugins/role-mode/
  .claude-plugin/plugin.json
  hooks/
    hooks.json
    mode_inject.py            # UserPromptSubmit hook
  prompts/modes/
    _common.md                # ALL MODES ルール
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
