# xml-wf — XMLワークフローシステム v2

タスクを単一責務のステップ列として XML に記述し、Python 製ランナー（`wfrun`）が
それを決定論的に実行します。各ステップは **独立した `claude -p` サブエージェント**
として動作し（完全なコンテキスト分離・ファイルベース I/O）、明示的な **ロール**
（エージェントが「誰」か）と任意の実行 **モード**（「どう」処理するか）の下で走ります。

このドキュメントは意図的にスキルディレクトリの外（`docs/`）に置いています。スキル
実行時にコンテキストへロードするものではなく、人間とエージェント双方のための参照
資料だからです。

- **README**（本ファイル）— あらゆる読者（開発者・ユーザ・人間・LLM）向けの概要と
  リファレンス。英語版は [README.md](README.md)。
- **[USER_GUIDE_ja.md](USER_GUIDE_ja.md)** — 非開発者の人間ユーザ向けの実践的な
  手順ガイド。英語版は [USER_GUIDE.md](USER_GUIDE.md)。

正典となる技術仕様はスキルの `references/spec.md` です。本 README はその要約であり、
食い違う場合は spec が優先します。

---

## 中核となる考え方

オーケストレーターは **LLM ではなく Python** です。XML は言語モデルが読むもので
はなく、`wfrun` がパースして制御フローを決定論的にたどります。LLM が関与するのは
厳密に次の 4 箇所だけです。

1. **ステップ実行** — `<step>` ごとに独立した `claude -p` サブプロセス 1 つ。
2. **LLM 条件判定** — `ask=` 属性（構造化出力で boolean + 理由を強制）。
3. **失敗診断** — `on-error="debug"` の debug ロール（任意）。
4. **動的リプラン** — `<replan>` ノードで継続ワークフローを生成する builder ロール
   （任意、ネストは 1 段まで）。

設計原則: **決定論**（同じ XML + 同じ入力 → 同じ経路）、**コンテキスト分離**
（ステップ間は会話を共有せず、受け渡しは変数とファイルのみ）、**ファイル中心 I/O**
（大きなデータはファイルパスとして移動）、**検証可能性**（閉じたスキーマ。実行前に
`wfrun validate` が静的検査）、**監査可能性**（すべてのプロンプト・応答・判定を
`runs/` に記録し、失敗地点から再開可能）。

---

## 前提

- **Python 3.12+**
- **uv**（Python はすべて `uv run` 経由で起動）
- **claude CLI v2.1.214+** — `run-cc` が必要とする（各ステップ・debug 診断・
  replan builder はすべて `claude -p` / `--json-schema` で実行。ロール定義は
  プロンプトに注入され、`--agent` は使わない）。`ask=` 判定も Claude Code 上で
  実行される場合はこの CLI を使う。Claude Code 以外では `wfrun ask` は代わりに
  **pi CLI** へ dispatch する（自動判定。後述 CLI リファレンスの `--backend` 参照）

ランナーは標準ライブラリのみで動作します。`build` モードは CLI を起動しません —
セッションの LLM 自身が XML を書き、`wfrun validate` で完結します（ただしロール
収集は `.claude/agents` / `$CLAUDE_CONFIG_DIR`、`tools=` の語彙は Claude Code の
ツール名を前提とします）。`run-cc` は Claude Code 向け、`run-llm` プロトコルは
サブエージェント機能を持つ任意のエージェント基盤で動作します。

**複数の `claude` インストール（Windows）:** wfrun は `PATH` が返す値をそのまま
起動するのではなく、実際に起動する `claude` 実行ファイルを自前で解決します。
npm の `.cmd`/`.bat` ランチャーをシェルなしで直接起動すると、Windows は
`claude` を `claude.cmd` に解決しないため起動に失敗するか、その shim 自身の
`cmd.exe` 層が `& | % ^ < >` や改行を含むプロンプトを壊すためです。専用の
環境変数は用意していません。複数の `claude` がインストールされている場合は
**`PATH` 上で先に現れるものが優先されます**。どちらを使うかは `PATH` の並び順
で選んでください。

---

## スキル構成

```
xml-wf/                        # スキル本体
├── SKILL.md                   # モード振り分け + 共通原則
├── references/
│   ├── spec.md                # 正典の制御構造仕様（v2）
│   ├── build.md               # Build モード手順
│   ├── run-cc.md              # Run（バッチ）+ Resume 手順
│   └── run-llm.md             # LLM オーケストレーション手順
└── scripts/
    ├── wfrun/                 # ランナー（Python 3.12・標準ライブラリのみ）
    │   ├── __main__.py        # CLI エントリ
    │   ├── modes/             # 同梱の role-mode プロンプト（execute, survey, …）
    │   └── model_map.json     # 難易度名 → 実モデルの束縛
    ├── examples/
    │   ├── hello.xml          # 検証済みの最小例
    │   ├── monthly_sales.xml  # 実運用規模の例
    │   ├── rules/             # 例が参照する外部ルール
    │   └── .claude/agents/    # 例が解決するロール（writer, debug）
    ├── tests/                 # 単体テスト（unittest）
    └── evals/prompt_smoke.py  # 任意のプロンプト層サンプラ（CLI を呼ぶ）
```

---

## 起動方法

ランナーは常に次のラッパー経由で起動します（`${CLAUDE_SKILL_DIR}` は Claude Code
上でスキルのディレクトリに解決されます）。

```bash
WFRUN="env PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts uv run python -m wfrun"
$WFRUN {validate|run|resume|plan|viz|prompt|record|poll|dispatch|wait|interp|eval|ask} ...
```

スキル経由では、4 つのモードを自然言語またはフラグから選択します。

| 引数 | モード | 内容 |
|---|---|---|
| `--build`、タスク説明、「ワークフロー化」 | **Build** | タスクを承認用のプラン表へ分解し、XML を生成・検証する。タスク自体は実行しない。 |
| `--run-cc`、`.xml` パス、「実行」 | **Run（バッチ）** | 検証 → `wfrun run` で決定論的に実行。既定の実行モード。 |
| `--resume`、run dir（`state.json` を含む） | **Resume** | 失敗した run を失敗地点から継続する。 |
| `--run-llm`、「対話的に実行」 | **LLM オーケストレーション** | エージェントが `wfrun` ヘルパーサブコマンドとファイルベース交換を用い、人間の監督下でステップごとに進行する。 |

---

## XML v2 の概観

検証済みの最小例（`scripts/examples/hello.xml`）:

```xml
<workflow name="hello" version="2" max="10" budget-usd="1.0">
  <param name="topic" default="the sea"/>
  <rules id="style">Write concisely. The poem must be 3 lines or fewer.</rules>

  <step id="s1_write" role="writer" mode="execute" rules="style"
        output="poem_path" output-type="value">
    <task>Write a short poem about {topic} and save it to the file
output/poem.txt. Return only the relative path of the saved file.</task>
  </step>

  <step id="s2_count" role="writer" output="line_count" output-type="value"
        schema='{"type":"object","properties":{"line_count":{"type":"integer"}},"required":["line_count"]}'>
    <task>Read the file {poem_path} and return its line count.</task>
  </step>

  <if ask="Does the content of file {poem_path} read as a poem?">
    <then>
      <step id="s3_note" mode="execute" tools="Write">
        <role>You are a careful file clerk who writes exactly what is
requested and never editorializes.</role>
        <task>Write exactly APPROVED to ./output/note.txt. Return only the
file path.</task>
      </step>
    </then>
  </if>
</workflow>
```

### 要素

- **`<workflow>`**（ルート）— `name`・`version="2"`・`max`（総ステップ実行回数の
  上限。暴走防止）が必須、`budget-usd` は任意。
- **`<param>`** — `wfrun run wf.xml -p key=value` で注入される実行時引数
  （`name` 必須、`required`・`default` は任意）。
- **`<rules id="…">`** — `rules=` 属性で参照したステップにのみ注入される
  プロンプト断片（`src` で外部ファイルを指せる）。
- **`<step>`** — 1 エージェントが実行する 1 タスク。`<task>`（必須）が指示本体、
  `<role>` はインラインロールを保持可能。属性: `id`・`role`・`mode`・`model`・
  `effort`・`output`・`output-type`・`schema`・`rules`・`tools`・`expect-file`・
  `retry`・`timeout`・`on-error`。
- **`<replan>`** — プランの一部を実行時に委ねる。builder エージェントが継続
  ワークフローを返し、`wfrun` が検証してインラインで実行する（ネスト 1 段まで）。
- **`<set>`** — 変数を補間（`value=`）または安全な式評価（`expr=`）で代入。
- **制御構造** — `<seq>`、`<if test=|ask=>`（`<then>`/`<else>`）、
  `<while test=|ask= max=>`、`<each items=|glob=|range= as=>`、`<parallel>`。

### ロール・モード・モデル

- **ロール**（各ステップに必須、いずれか一方）: **名前付きロール**（`role="name"`
  が `.claude/agents/*.md` 定義に解決され、その本体が注入される）または
  **インライン `<role>`** 子要素（その場で書く 1〜3 文）。名前付きロールは
  frontmatter の `model`/`tools` を伴い、インラインロールは `tools=` を明示すべき。
- **モード**（`mode=`、任意）: `mode:<name>` とそのルールとして注入される処理規律。
  自律モードのみ: `debug`・`execute`・`plan`・`review`・`review-dev`・`survey`、
  さらにエイリアス `verify` → debug、`implement` → execute。
- **モデル**（`model=`）: デプロイ名ではなく **難易度クラス**。正典の名前は
  `haiku`（機械的）・`sonnet`（標準・既定）・`opus`（設計/診断/レビュー）。
  `scripts/wfrun/model_map.json` がディスパッチ時に実モデルへ束縛する。同梱の
  マップは恒等（設定ゼロ）。

ステップ内のプロンプト優先度: **Mode > Rules > Task > Role**。

---

## CLI リファレンス

```
wfrun validate <wf.xml> [--json] [--no-role-check] [--as-child] [--defined-vars VARS_JSON]
wfrun run      <wf.xml> [-p k=v ...] [--run-dir D] [--runs-root runs] [--permission-mode acceptEdits]
wfrun resume   <run_dir> [--base-dir D] [--permission-mode ...]
wfrun plan     <wf.xml>                 # ステップツリーを表示（実行なし）
wfrun viz      <wf.xml> [--out FILE]    # 制御フローの mermaid フローチャート
```

`run-llm` オーケストレーション用のヘルパーサブコマンド（タスク内容は呼び出し側を
経由しない）:

```
wfrun prompt <wf.xml> <id> --vars V --out PROMPT [--result RESULT] [--fix TEXT] [--attempt N]
wfrun record <wf.xml> <id> --result RESULT --vars V [--log LOG] [--reply LINE]
wfrun poll   <handle.json>              # B層: done(0) / running(10) / deadline-exceeded(11)
```

レイヤが決めるのは**誰がステップを実行するか**である。A層は claude CLI、B層は
オーケストレータ自身の subagent 機能で実行する。判定は claude CLI の有無しか見ないため、
Claude Code 以外の harness でも claude が入っていれば A層 が選ばれる。その harness 上で
実行させたい場合は B層 を明示的に選ぶこと。B層 は subagent ツールを一切持たない harness
（Pi）でも動作する。配送形は `references/run-llm.md` を参照。

A層（`claude --version` が成功する環境向け。サブエージェント委譲なしで `wfrun` 自身が
detach したラッパープロセス経由で `claude -p` を呼ぶ。詳細は `references/run-llm.md`）:

```
wfrun dispatch <wf.xml> <id> --vars V --run-dir D [--permission-mode M] [--fix TEXT] [--new-cycle]
wfrun wait     <handle.json> --max SEC --vars V [--log LOG]
               # ok(0) / error: <class>(1) / running(10) / aborted(3)
```

```
wfrun interp <text> --vars V            # {var} 参照を補間
wfrun eval   <expr> --vars V            # test= 式を評価 → true/false
wfrun ask    <question> [--vars V] [--model haiku] [--backend auto|cc|pi] [--quiet] [--log LOG]
```

`--backend`（既定 `auto`）: 判定を `claude` CLI（`cc`）または `pi` CLI（`pi`）の
どちらへ dispatch するかを指定する。`auto` は `CLAUDE_CODE_SESSION_ID`（非空なら
`cc`、空なら `pi`）から実行中の harness を判定する — PATH 上にどのバイナリが
あるかは見ない。Pi backend には構造化出力を強制するフラグが無く（プロンプト指示
＋二段パースで代替）、`cost_usd` は常に `0.0` になる。`--log` の各行には
どちらの backend が動いたかが `"backend"` として記録される。

注意:
- `--permission-mode` は、解決後のツールが書き込み可能なステップにのみ転送される。
  survey/review ステップには読み取り専用の `tools=` を与え、広げた権限が届かない
  ようにする。
- 名前付きロールの解決・rules 相対パス・サブプロセス cwd はすべて **XML ファイルを
  含むディレクトリ** を基準とする。そのディレクトリは Claude の config tree
  （`~/.claude`、および設定時は `$CLAUDE_CONFIG_DIR` — 両方が保護対象）配下に
  あってはならない。

---

## run ディレクトリと再開

```
runs/<name>_<YYYYMMDD-HHMMSS>/
├── workflow.xml     # 実行時に取ったスナップショット（resume はこれを読む）
├── params.json
├── state.json       # {status, vars, step_count, cost_usd, error}
├── events.jsonl     # 追記専用の実行ログ
├── outputs/<id>.md  # output-type=file の応答本文
├── replans/<id>_<nn>.xml
└── steps/<id>_<attempt n>/{system.md, prompt.md, result.json, stderr.log}
```

`wfrun resume runs/<dir>` は記録済みの成功を再生し（ステップ結果と `ask=` 判定を
再実行せずに再利用）、最初の不一致または未記録の地点から実際の実行を始めます。
失敗ステップを直すには、再開前に run dir 内の `workflow.xml` を編集します
（成功済みの定義は変更しないこと）。

---

## エラーハンドリング

検出優先度: 非ゼロ終了 / `is_error` → タイムアウト → 応答本文が `ERROR:` で始まる
→ 先頭行が `[BLOCKED:` で始まる（mode/rules 拒否）→ `schema` を与えたのに構造化
出力が返らない → `expect-file` のパスが存在しない。

処理: 決定論的 **`retry`**（同一プロンプトで再実行）→ その後 `on-error`:
`fail`（停止。回復経路は resume）、`ignore`（記録して継続）、`debug`（debug ロール
が `{action: RETRY|FAIL, reason, fix_instruction?}` で診断。RETRY は fix を付けて
ちょうど 1 回だけ再実行）。

ファイルを生成するステップには必ず `expect-file=` を与えてください。
`ERROR:`/`[BLOCKED:` プロトコルは *素直な* 拒否しか捕捉しませんが、`expect-file`
は成果物そのものを検証します。

---

## 例

- **`scripts/examples/hello.xml`** — 最小・自己完結。ロール（`writer`・`debug`）は
  `scripts/examples/.claude/agents/` に同梱されており、`examples/` ディレクトリから
  そのまま検証・実行できます。
- **`scripts/examples/monthly_sales.xml`** — 実運用規模（月次売上分析）。実行する
  プロジェクトの `.claude/agents/` に `analytic-sql-coder`・`faithful-operator`・
  `data-explainer` の各ロールが定義されている前提で、これらのロール定義は
  **同梱されていません**。実行前に `--no-role-check` で検証するか、ロールを用意して
  ください。

---

## 開発

```bash
# scripts/ から
uv run python -m unittest discover -s tests      # 決定論的な単体テスト
uv run python -m wfrun validate examples/hello.xml
uv run python evals/prompt_smoke.py              # 任意。プロンプト層をサンプリング、CLI を呼ぶ
```

プロンプト層（`ERROR:`/`[BLOCKED:` プロトコル）は確率的で単体テストの外です。
`modes/*.md` やプロンプト組み立てを編集したら `prompt_smoke.py` でサンプリングし、
前後の遵守率を比較してください。
