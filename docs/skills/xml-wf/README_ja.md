# xml-wf — XMLワークフローシステム v2

タスクを単一責務のステップ列として XML に記述し、Python 製ランナー（`wfrun`）が
それを決定論的に実行します。各ステップは **独立した `claude -p` サブエージェント**
として動作し（完全なコンテキスト分離・ファイルベース I/O）、任意の **ロール**
（エージェントが「誰」か）と実行 **モード**（「どう」処理するか）の下で走ります。

このドキュメントは意図的にスキルディレクトリの外（`docs/`）に置いています。スキル
実行時にコンテキストへロードするものではなく、人間とエージェント双方のための参照
資料だからです。

- **README**（本ファイル）— あらゆる読者（開発者・ユーザ・人間・LLM）向けの概要と
  リファレンス。英語版は [README.md](README.md)。
- **[USER_GUIDE_ja.md](USER_GUIDE_ja.md)** — 非開発者の人間ユーザ向けの実践的な
  手順ガイド。英語版は [USER_GUIDE.md](USER_GUIDE.md)。
- **[AUTHORING_CONTRACT.md](AUTHORING_CONTRACT.md)** — スキル編集者向けの保守義務
  （英語のみ）: 3面（cc/pi/run-llm）整合性、mode-orchestrator との関係（両スキルが
  そろえている再投入ゲートを含む）、`modes/` 伝播の参照先。

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
- **エージェント CLI が少なくとも1つ**。どちらを使うかでワークフローに書ける内容が変わる:
  - **claude CLI v2.1.214+** — `cc` backend が必要とする（各ステップ・debug 診断・
    replan builder はすべて `claude -p` / `--json-schema` で実行。ロール定義は
    プロンプトに注入され、`--agent` は使わない）
  - **pi CLI** — `pi` backend はこれだけで動作し、claude のインストールを必要としない。
    ただし pi では強制できないため `schema=` と `on-error="debug"` を起動時に拒否する
    （書き換え方は `references/run-pi.md`）
  - `ask=` 判定も `run --backend` と同じ自動判定に従う

ランナーは標準ライブラリのみで動作します。`build` モードは CLI を起動しません —
セッションの LLM 自身が XML を書き、`wfrun validate` で完結します（ただしロール
収集は `.claude/agents` / `$CLAUDE_CONFIG_DIR`、`tools=` の語彙は Claude Code の
ツール名を前提とします）。`run-cc` は Claude Code 向けです。`run-llm` が動くのは
Claude Code と Pi だけで、委譲はすべてホストのネイティブなサブエージェント機能
（Claude Code では Agent ツール、Pi では `subagent` ツール）を通り、ステップの
実行のために `claude` や `pi` のプロセスを起動することはありません。Pi ではその
ツールを、先にセットアップする拡張が提供します（[Pi setup](#pi-setup) を参照）。
`run-llm` が今も起動する CLI は `wfrun ask` だけです。

**複数の `claude` インストール（Windows）:** wfrun は `PATH` が返す値をそのまま
起動するのではなく、実際に起動する `claude` 実行ファイルを自前で解決します。
npm の `.cmd`/`.bat` ランチャーをシェルなしで直接起動すると、Windows は
`claude` を `claude.cmd` に解決しないため起動に失敗するか、その shim 自身の
`cmd.exe` 層が `& | % ^ < >` や改行を含むプロンプトを壊すためです。専用の
環境変数は用意していません。複数の `claude` がインストールされている場合は
**`PATH` 上で先に現れるものが優先されます**。どちらを使うかは `PATH` の並び順
で選んでください。

---

## Pi setup

これが必要なのは Pi 上の `run-llm` だけで、Claude Code には要りません。Pi では、
`run-llm` はすべての委譲（ステップ、debug 診断、`<replan>` の builder、decider）を
ネイティブな `subagent` ツール経由で行い、そのツールは Pi の subagent 拡張
（`@gigamori/pi-subagent`）が提供します。このツールが無いセッション
（`delegation_contract` ツールが無い）では実行できません。`run-llm` は run dir を
作る前に停止し、ここを指すメッセージを返します。`--run-pi` と `--run-cc` に拡張は
不要です。

**入れるもの。** `pi-extensions` リポジトリの `subagent` パッケージと、その依存の
`@gigamori/pi-child-session-core`。どちらも npm には公開されていない（2026-11-06 に
確認）ので、ビルド済みのコピーを agent ディレクトリの `extensions/` に置きます。
以下の `<agent dir>` は `~/.pi/agent`、`$PI_CODING_AGENT_DIR` が設定されていれば
その値です。

`pi-extensions` のクリーンなチェックアウトを、コミット
`fix(child-session-core): Key top-level package copies by name` 以降でビルドします
（`npm ci`、続いて `npm run build`）。これより古い core では、コピーが `local:dist`
として登録され、すべての子セッションに引き継がれるので、子がさらに委譲できてしまい
ます（スタブ provider で測定: 古い core ではモデル呼び出し 5 回、修正済みでは 3 回）。
Pi Studio も、同梱する core に同じ修正が要ります。

```bash
SRC=<built pi-extensions checkout>
X=<agent dir>/extensions
D="$X/subagent"
C="$D/node_modules/@gigamori/pi-child-session-core"
rm -rf "$D"
mkdir -p "$C"
cp -r "$SRC/packages/child-session-core/dist" "$C/dist"
cp "$SRC/packages/child-session-core/package.json" "$C/package.json"
cp -r "$SRC/packages/subagent/dist" "$D/dist"
cp "$SRC/packages/subagent/package.json" "$D/package.json"
mkdir -p "$X/child-session"
printf '{"explicitModels":"registry"}\n' > "$X/child-session/config.json"
grep -c readDeclaringPackageName "$C/dist/catalog-cli.js"   # expect 2
```

そのあと Pi を再起動するか、起動中のセッションで `/reload` を実行します。Windows では
上のリストを Git Bash で実行してください。

- リストは `package.json` を最後にコピーします。インストールの途中で起動した Pi が
  未完成のパッケージを見ないようにするためです。リンクではなくコピーなので、あとで
  拡張を外すときに消えるのはコピーだけです。リストは `rm -rf "$D"` で始まるので、
  再実行すると、古いコピーの中に 2 つ目の `dist/` を作らず、古いコピーを置き換えます。
  消えるのはこのパッケージのコピーだけで `child-session/` は消えませんが、すぐ後の `printf` 行が
  `child-session/config.json` を書き直すので、再実行すると追加した `profiles` は消えます。
- `child-session/config.json` は `{"explicitModels":"registry"}` です。これが無いと
  host は `model` の明示指定を拒否します（`resolve` が `blocked` を返し、
  "Explicit model channel is not configured." と出る）。profile を持たない tier に
  対して `run-llm` が出すのがこの要求です。`profiles` は置かないので、各 tier
  （`basic`・`pro`・`ultra`）は `absent` になり、`run-llm` は `model_map.json` の
  `llm` 表がその tier に与えるモデルを要求します。`profiles` を足す（書式は
  `subagent` の README の "CLI configuration"）と、設定済みの tier は呼び出し単位の
  `effort` profile として要求され、モデルは host の設定が選びます。このファイルは
  child-session を使うすべての consumer で共有されます。
- セットアップの確認は、Pi のセッションで `delegation_contract` を
  `{"operation":"observe"}` で呼びます。`routes.native.state` が `available` になる
  はずです。
- `available` は経路があることを示すだけで、モデルが解決できることは示しません。
  `resolve` には、`run-llm` が要求するモデルが host のカタログにあり、認証が通って
  いることも必要です。同梱の `model_map.json` では、委譲が要求するモデルは
  `haiku`、`sonnet`、`opus` です（その行が名指す tier `basic`、`pro`、`ultra` に
  対応します）。Anthropic の認証も対応する provider 拡張も無い Pi では
  `unavailable`（"No unique compatible binding in the host catalog snapshot."）が
  返り、`run-llm` は run dir を作る前にそこで止まります。`/login` で Anthropic に
  ログインするか、その provider を導入するか、`profiles` を設定してください。
  `ask=` はこの確認の外です。workflow がそこに達すると、`wfrun ask` が Pi を
  `--model haiku` で起動するので、Pi 自体で `haiku` が動く必要があり、`profiles`
  では補えません。

**導入後に変わること。**
- 拡張はマシン全体に効きます。この agent ディレクトリを読む Pi プロセスはすべて
  読み込みます。`run-pi` と mode-orchestrator の CLI 経路の `pi -p` の子も含みます。
  `run-pi` は `--no-extensions` を渡さず（モデル名の解決に拡張が要るため）、
  `--tools` は `tools=` を持つステップにだけ渡します。mode-orchestrator の起動は
  どちらも渡しません。そのように起動した子は `subagent` ツールを持ちます。修正済みの
  core では、その子の子には渡らないので、深さが増えるのは 1 段までです。これは
  ソースの読解で、`pi -p` を実行して確かめたものではありません。
- 修正済みの core を同梱した Pi Studio では、子セッションに `subagent` ツールが
  ありませんでした（1 回の観測）。Pi Studio で `run-llm` が動くかは検証していません。

外すには `rm -rf <agent dir>/extensions/subagent
<agent dir>/extensions/child-session` を実行し（先に `child-session/` に自分で足した
ファイルが無いか確認）、`/reload` します。セットアップは `settings.json` に触れません。

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
$WFRUN {validate|run|resume|plan|viz|prompt|record|decide|models|poll|stop|judge|dispatch|wait|interp|eval|ask} ...
```

スキル経由では、4 つのモードを自然言語またはフラグから選択します。

| 引数 | モード | 内容 |
|---|---|---|
| `--build`、タスク説明、「ワークフロー化」 | **Build** | タスクを承認用のプラン表へ分解し、XML を生成・検証する。タスク自体は実行しない。 |
| `--run-cc`、`.xml` パス、「実行」 | **Run（バッチ）** | 検証 → `wfrun run` で決定論的に実行。既定の実行モード。 |
| `--resume`、run dir（`state.json` を含む） | **Resume** | 失敗した run を失敗地点から継続する。 |
| `--run-llm`、「対話的に実行」 | **LLM オーケストレーション** | エージェントが `wfrun` ヘルパーサブコマンドとファイルベース交換を用い、人間の監督下でステップごとに進行する。このモードではステップの `effort=` は適用されない（Agent ツールは effort を受け取らず、Pi の `subagent` 呼び出しの `effort` は tier の profile を選ぶもので、ステップの属性ではない）。Pi では subagent 拡張が要る（[Pi setup](#pi-setup) を参照）。 |

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
  上限。暴走防止）が必須、`budget-usd`・`decider`・`decider-model` は任意。
- **`<param>`** — `wfrun run wf.xml -p key=value` で注入される実行時引数
  （`name` 必須、`required`・`default` は任意）。
- **`<rules id="…">`** — `rules=` 属性で参照したステップにのみ注入される
  プロンプト断片（`src` で外部ファイルを指せる）。
- **`<step>`** — 1 エージェントが実行する 1 タスク。`<task>`（必須）が指示本体、
  `<role>` はインラインロールを保持可能。属性: `id`・`role`・`mode`・`model`・
  `effort`・`output`・`output-type`・`schema`・`rules`・`tools`・`expect-file`・
  `retry`・`decider`・`decider-model`・`timeout`・`on-error`。
- **`<replan>`** — プランの一部を実行時に委ねる。builder エージェントが継続
  ワークフローを返し、`wfrun` が検証してインラインで実行する（ネスト 1 段まで）。
- **`<set>`** — 変数を補間（`value=`）または安全な式評価（`expr=`）で代入。
- **制御構造** — `<seq>`、`<if test=|ask=>`（`<then>`/`<else>`）、
  `<while test=|ask= max=>`、`<each items=|glob=|range= as=>`、`<parallel>`。

### ロール・モード・モデル

- **ロール**（任意、指定するなら一方のみ）: **名前付きロール**（`role="name"`
  が `.claude/agents/*.md` 定義に解決され、その本体が注入される）または
  **インライン `<role>`** 子要素（その場で書く 1〜3 文）。名前付きロールは
  frontmatter の `model`/`tools` を伴い、インライン／ロール無しのステップは
  `tools=` を明示すべき。`mode=`/`rules=` で規律が既に決まっているなら、埋め草の
  ペルソナを捏造するより省略するのが良い既定。
- **モード**（`mode=`、任意）: `mode:<name>` とそのルールとして注入される処理規律。
  自律モードのみ: `debug`・`execute`・`plan`・`review-doc`・`review-dev`・
  `review-process`・`survey`、さらにエイリアス `verify` → debug、`implement` → execute。
- **モデル**（`model=`）: デプロイ名ではなく **難易度クラス**。正典の名前は
  `basic`（機械的）・`pro`（標準・既定）・`ultra`（設計/診断/レビュー）。
  `scripts/wfrun/model_map.json` がディスパッチ時に実モデルへ束縛する。同梱の
  束縛は設定不要で使える（各難易度クラスが既定で実モデルに束縛済み）。

ステップ内のプロンプト優先度: **Mode > Rules > Task > Role**。ロールを宣言しない
ステップでは **Mode > Rules > Task**。

---

## CLI リファレンス

```
wfrun validate <wf.xml> [--json] [--no-role-check] [--backend auto|cc|pi|llm]
                        [--as-child] [--defined-vars VARS_JSON]
wfrun run      <wf.xml> [-p k=v ...] [--run-dir D] [--runs-root runs] [--permission-mode acceptEdits] [--backend auto|cc|pi] [--inherit-model M]
wfrun resume   <run_dir> [--base-dir D] [--permission-mode ...]
wfrun plan     <wf.xml>                 # ステップツリーを表示（実行なし）
wfrun viz      <wf.xml> [--out FILE]    # 制御フローの mermaid フローチャート
```

`run` の `--backend` はステップを実行する CLI を選ぶ: `cc`（claude CLI）または
`pi`。既定の `auto` は `CLAUDE_CODE_SESSION_ID` を読み（非空→`cc`、空→`pi`）、
呼び出し側の記憶に頼らずコード側で判定する。両 backend は等価ではない: pi backend は
`schema=` と `on-error="debug"` をプロセス起動前に拒否する（pi では強制できないため）。
書き換え方と挙動差（retry の二重積算・`transient` クラス無し・`budget-usd` がほぼ無効・
`tools=` 名の変換・`Bash(git:*)` のような引数 specifier がツール全体へ広がること
（pi にコマンド単位のマッチ機構が無いため））は `references/run-pi.md` を参照。`resume` は
run ディレクトリに記録された backend を使い再判定しないため、1つの run が2つの CLI に
またがることはない。

`validate --backend llm` は `run-llm` が渡す指定。XML 自体を検査するだけで CLI は
起動せず、モデル名を catalog と照合することもない。`run-llm` はモデルを
オーケストレータ自身の subagent 機能を通して解決するため。

`--inherit-model` は、自前の model 指定を持たないステップ（`model=` 属性もロールの
frontmatter 既定も無い）に使うモデルを与える。wfrun は独立したプロセスで動作し、
呼び出し元セッションのモデルを検出する経路が無いため、値は呼び出し側のスキルが明示的に
渡す。渡す値は具体的なモデル識別子であり、`model_map.json` を通す canonical な難易度
クラスではない（そのまま使われる）。省略はエラーではないが、その場合は backend CLI 自身が
選ぶモデルに委ねられる。これはマシンごとに異なり、文書化もされておらず、CLI が「既定」と
呼ぶものとも限らない（pi では、pi 自身の `defaultModel` すら使わず、呼び出し元セッションとは
無関係の有効化済み provider が選ばれる挙動を実測）。該当するステップは
run 開始時に `note:` 行で列挙される。`resume` は backend と同じく run ディレクトリに
記録された値を継承し、`resume` 自身に上書きフラグは無い。

`run-llm` オーケストレーション用のヘルパーサブコマンド（タスク内容は呼び出し側を
経由しない）:

```
wfrun prompt <wf.xml> <id> --vars V --out PROMPT [--result RESULT] [--fix TEXT] [--new-cycle] [--redispatch JUDGE_REPLY]
wfrun record <wf.xml> <id> --result RESULT --vars V [--log LOG] [--reply LINE] [--answer FILE] [--debug-reply LINE] [--decider human|llm]
wfrun poll   <handle.json> [--wait]     # done(0) / running(10) / deadline-exceeded(11)
wfrun decide <wf.xml> <id> --result RESULT   # 未決の decision を llm decider に 1 回だけ委譲する
wfrun models <wf.xml>                   # 各委譲が出すモデル要求
wfrun stop   <handle.json> --facet cc --task-stop stopped|completed|failed [--observe 1..60]
                                        # Claude Code のみ: done(0) / confirmed(12) / unconfirmed(13)
wfrun judge  <wf.xml> <id> --result RESULT   # 中断した試行の判定役を 1 回だけ発行する
```

`--result` を付けると、`prompt` はステップの新しい試行を開始する。試行ごとに専用の
結果ファイルと sentinel を与え、新しい試行を始めてはならないときは拒否する（exit 1）。
あわせて、その試行に固有の `description=wfrun <id> cNN aNN <token>` も表示する。
Claude Code では、これを Agent 呼び出しの description にそのまま渡すこと。
- `record` はそれを手掛かりに、その subagent の transcript と、その subagent が起動したすべての agent の transcript を見つけ、拒否されたツール呼び出しを調べる。
- 拒否が見つかれば、結果ファイルの内容にかかわらずステップは `error: denied` になる。ただし、試行に停止の記録があり結果が完成しなかった場合は、`record` は試行を `aborted` と記録し、検査の判定を 2 行目の `deny-check: DENIED <n> (details: <transcript>)` として表示する。
- transcript が見つからなければ、判定の次の行に `deny-check: NO-TRANSCRIPT: <理由>` が表示され、そのステップが clean として記録されることはない。
- 調べた結果は `steps.log` に `deny_check` として残る。

`models` は、ワークフローの各委譲が出すモデル要求を制御の事実として出力する。
`harness=cc|pi` の行（`CLAUDE_CODE_SESSION_ID` が設定されていて空でなければ `cc`）の
後に、要求ごとに 1 行。`tier=`（名前が `basic`・`pro`・`ultra` を表すときだけ付く）、
`model=`、`sites=`（step と `<replan>` の id。llm decider は `<id>:decider`）を持つ。
`prompt` の dispatch 行と `decide` の行も同じ `tier=` と `model=` を出し、`decide` の
行はさらに、その委譲に渡す `timeout=` を出す。

`run-llm` には選ぶべきレイヤも CLI の probe も無い。ステップ・debug 診断・
`<replan>` の builder・decider はすべて、オーケストレータ自身のネイティブな委譲機能で
実行するので、ほかに何が入っていても、オーケストレーションしているエージェント上で
動く。Claude Code ではそれが Agent ツールである。Pi では `subagent` ツールである。
Preparation はまず、host がそれを公開していること（`delegation_contract`）と、
`wfrun models` の全モデル要求が resolve できることを確かめ、そうでなければ run dir を
作る前に停止する（[Pi setup](#pi-setup) を参照）。`claude -p` や `pi -p` のプロセスへ
フォールバックすることはなく、`--run-cc` / `--run-pi` へ自動で移ることもない。`ask=` だけが
例外で、`wfrun ask` は自分で CLI を起動し、委譲ではなく、委譲が使えない環境でも動く。
Pi で中断したステップは記録されて run が止まる。`subagent` の子の停止を観測するものは
無いので、確認された停止も判定役も再投入も無く、その run dir は再開できない。選択肢は
`--run-pi`、`--run-cc`、新しい run である。Claude Code で期限を過ぎたあとの停止・判定役・
再投入の扱いは、同じ文書の "On abort" にある。

`dispatch` と `wait` は `run-llm` の一部ではなく、`references/run-llm.md` にも書かれて
いない。ステップを委譲機能ではなく claude CLI で実行するコマンドで、`wfrun` 自身が
detach したラッパープロセス経由で `claude -p` を呼ぶ。

```
wfrun dispatch <wf.xml> <id> --vars V --run-dir D [--permission-mode M] [--fix TEXT] [--new-cycle]
wfrun wait     <handle.json> --max SEC --vars V [--log LOG]
               # ok(0) / error: <class>(1) / running(10) / aborted(3)
```

ツール呼び出しが拒否された場合、`wait` は `error: denied (details: <file>)` を
表示する。試行が失敗したあとの `dispatch` は `run` と同じ retry/debug の扱いを
適用し、その扱いでは行わない再実行（`denied` など retry しないクラス、使い切った
`retry`、2 回目の fix 付き再実行）を拒否する。拒否メッセージは代わりにすべきことを
示す。`--fix` は `wait` が `debug: RETRY` を表示した直後だけ受け付ける。
`--new-cycle` は新しい `<while>`/`<each>` 反復のときだけ渡すこと。

```
wfrun interp <text> --vars V            # {var} 参照を補間
wfrun eval   <expr> --vars V            # test= 式を評価 → true/false
wfrun ask    <question> [--vars V] [--model basic] [--backend auto|cc|pi] [--quiet] [--log LOG]
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
└── steps/<id>_<attempt n>/{system.md, prompt.md, result.json, stream.jsonl, stderr.log}
```

`wfrun resume runs/<dir>` は記録済みの成功を再生し（ステップ結果と `ask=` 判定を
再実行せずに再利用）、最初の不一致または未記録の地点から実際の実行を始めます。
失敗ステップを直すには、再開前に run dir 内の `workflow.xml` を編集します
（成功済みの定義は変更しないこと）。

この構成はバッチ run のものです。`run-llm` の run は、代わりに
`<ws>/tmp/runs/<name>_<ts>-llm/`（`<ws>` はセッションを起動したディレクトリ）へ
ファイルを置きます: `vars.json`、`steps.log`、`steps/<id>_cNN_aNN_*`（試行ごとに
1 組のファイル）、`replans/`、`decisions/`、そして run が git worktree を作った場合は
`worktrees.json`。`state.json` と `events.jsonl` は無く、`wfrun resume` で再開する
ことはできず、run がこれを削除することもありません。

---

## エラーハンドリング

検出優先度:
1. 拒否されたツール呼び出し（`denied`）: いずれかの result イベントの `permission_denials` に載っている。ステップが起動した agent 内の拒否も含む。
2. 非ゼロ終了 / `is_error`。
3. タイムアウト。
4. 応答本文が `ERROR:` で始まる。
5. 先頭行が `[BLOCKED:` で始まる（mode/rules 拒否）。
6. 本文が `DECISION:` 要求を運んでいる。これはエラーではない（後述）。
7. `schema` を与えたのに構造化出力が返らない。
8. 成功するはずの result に `permission_denials` フィールドがない（`unchecked`: 拒否の検査ができなかった）。
9. `expect-file` のパスが存在しない。

`result.json` には最後の result イベントだけが入り、`stream.jsonl` には stream 全体が
入る。`denied` の原因となった拒否は、それより前のイベントに載っていることがある。
`denied` と `unchecked` は retry されず、debug にも回らない。

処理: 決定論的 **`retry`**（同一プロンプトで再実行）→ その後 `on-error`:
`fail`（停止。回復経路は resume）、`ignore`（記録して継続）、`debug`（debug ロール
が `{action: RETRY|FAIL, reason, fix_instruction?}` で診断。RETRY は fix を付けて
ちょうど 1 回だけ再実行）。

ファイルを生成するステップには必ず `expect-file=` を与えてください。
`ERROR:`/`[BLOCKED:` プロトコルは *素直な* 拒否しか捕捉しませんが、`expect-file`
は成果物そのものを検証します。

**判断要求（decision request）。** 本当に曖昧なフォークに当たったステップは、
推測する代わりに `DECISION:`（選択肢・推奨・作業状態）を発行します。既定の
`decider="human"` では run が停止し（`awaiting-decision`、exit 4）、
`wfrun resume <dir> --answer <step>=<file>` で継続します。`decider="llm"` なら
裁定モデル（`decider-model`、既定 `ultra`）がその場でフォークを解決します——
1 ステップ訪問につき裁定は最大 2 回、不可逆・外向き・目的変更のフォークは human へ
エスカレーションされます。両 backend で動きます（claude では schema 強制の裁定、
pi ではテキスト裁定）。`references/spec.md` の "Decision requests" を参照。

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
