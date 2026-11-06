# mode-orchestrator — ユーザーガイド

`mode-orchestrator` スキルの利用者向けガイド。**todolist**（指示のリスト）と各ステップに必要な context をすでに含むドキュメントを読み、各ステップを role-mode の `mode:` / `role:` ヘッダを付けた隔離 subagent ターンとして実行する。LLM 向けの仕様は本スキルの `SKILL.md`（`skills/mode-orchestrator/` 配下）にある。workflow spec の書き方は同じフォルダの `WORKFLOW_SPEC_AUTHORING.md` を参照。（英語版: `USER_GUIDE.md`）

## 何をするか

- todolist + 関連 context を含む1つのドキュメントを受け取る。
- 各ターンは1つの責務だけを担う。**mode**（任意で **role**）を選び、その mode の NEVER/DO ルールを載せたプロンプトを組み立て、**1つの隔離 general-purpose subagent ターン**として実行する。ステップは mode、role、model、authority、deliverable が変わる箇所で分割する。複数ファイルや複数ツール呼び出しだけでは分割しない。1ターンにつき 1 mode（role は最大1つ）、混在なし。
- 実行されるのは **autonomous** mode のみ。interactive mode は実行せず、ネイティブ実行の提案として提示する。
- 各ターンは deliverable ファイルを書き、後続ターンは先行ファイルをパスで受け取る。

構造化されていないタスクをゼロから分解することは**しない** — todolist は入力に既に存在している必要がある。

## いつ使うか

todolist を含む設計ドキュメント・plan・handoff を指し、mode ごとに orchestrate / run / execute するよう依頼する。典型的なトリガー:

```
path/to/plan.md に mode-orchestrator を使って
```

## 前提 — Step 0 ゲート

生成の前に入力をゲートし、以下の場合は**リジェクト**する:

- 識別可能な指示リストが無い、
- ステップが曖昧で mode にマップできない、
- ステップ実行に必要な context が欠けている。

リジェクト時は何が欠けているかを名指しし、十分な todolist の提供を求める。ギャップを推測で埋めない。

**headless で起動する場合の前提が 1 つある**（`claude -p`、ラッパースクリプト、入れ子 run）。ゲートの前に、スキルはどのハーネス上に居るかを判定する小さなシェルコマンドを 1 回実行する。Claude Code ではこのコマンドに許可が要り、対話セッションならその場で承認できるが、**headless は尋ねる相手が居ないため拒否され、run はそこで止まる**。その run が読む設定でこの 1 コマンドを許可するか、`--allowedTools` で渡すこと。正確な文字列は `references/harness-cc.md` にある。ただし、これで足りるとは限らない: ターン watchdog の起動コマンド（`exec bash …/watchdog.sh …`）は、完全一致の allow rule があっても `--permission-mode default` と `acceptEdits` で拒否され（Claude Code 2.1.318、2026-11-06、各 1 回）、`--permission-prompts none` でも拒否された（2.1.286、2026-10-02、1 回）。その run は最初のターンで `blocked` になる。`--permission-mode auto` では実行され、run は完走した（headless と対話で各 1 回）。Claude Code では再投入ゲートのスクリプトも `node` で動き、`node` が無ければ中断したターンは再実行されない。通常の対話実行では何もしなくてよい。

## 起動 — フラグ

| フラグ | 効果 |
|---|---|
| _(なし)_ | turn plan を提示し、実行前に承認を待つ。 |
| `--auto` | 承認ゲートを飛ばし、plan が確定済みの内容について全ターンを承認なしで実行する。実行時に新たに導出した model 解決（自然言語・略称・別名・接頭辞・系列）は利用者の明示承認が必要 — headless では開始前に停止して解決内容を見せる。 |
| `--roles` / `--roles=always` | 各ターンに適合する role を推論して付与する。デフォルト: role は推論しないが、todolist に明示された role は honor する。 |
| `--workflow=<name>` | workflow spec（`workflows/<name>.md`）をタスク種別のガイダンスとして読み込む。todolist 内で spec 名を宣言しても同様に honor される。デフォルト: spec なし — todolist の指定どおりに実行。 |
| `--decider=llm` | `needs-decision` の分岐を挿入されたターンに裁定させる。デフォルトは `human` — run はステップ境界で止まり、質問を提示する。ただしモデルが書いた指示どうしの分岐は、どちらの値でも挿入されたターンが裁定する。後述の decision ループを参照。 |

フラグは意図的に `--` 形式を使う。`mode:` / `role:` のコロン接頭辞は使わない（role-mode フックに捕捉されるため）。

## Mode

**Autonomous**（subagent ターンとして実行）: `survey`, `plan`, `execute`, `debug`, `review-process`, `review-dev`, `review-doc`。エイリアス: `verify` → `debug`, `implement` → `execute`。

**Interactive**（実行しない — ネイティブ実行の提案として提示）: `ask`, `discuss`, `brainstorm`, `organize`, `explain`。これらは autonomous subagent には提供できない、人間とのライブなやり取りを必要とする。

mode は hybrid で決定: ステップが mode を名指ししていれば honor、なければステップ内容から適合 mode を推論する。

## ターンごとの effort と model

`references/execution-profiles.md` は `basic`、`pro`、`ultra` を harness ごとの
model へ対応付ける。これは必須の skill 既定 binding であり、3つの model 要求元の
最後に置かれる（delegation contract の model 要求源の節参照）。委譲はホストの native subagent 経路だけを通る（CLI や自由 shell への迂回はせず、restart judge も同じ）。
orchestrator はどのターンを委譲する前にも、その経路が利用可能かを読み、
`unavailable`（または model を固定できないビルド）なら、ホストの理由を引用して委譲前に run を停止する。
許可状態は読まない: 子が親より広い権限を持たないのは、この経路がホストのガードを子へ運ぶためであり、
呼出し時の拒否は事後に分類する（`blocked`）。

番号付き todolist の各ステップには、ordinal の後の最初の物理行のどこかに
`(model: VALUE)` を最大1つ、`(effort: basic|pro|ultra)` を最大1つ、
`(workflow-step: ID)` を最大1つ置ける。継続行は task text である。空の値、
重複キー、不正な effort、active workflow に無い ID、または active workflow 内の重複 ID は、承認または delegation の前に
run を `blocked` にする。認識された metadata は残りの instruction を分類する前に除去し、
そのステップから分割された全ターンへコピーする。分割した責務ごとに異なる明示値が必要なら、
番号付きステップを分ける。

effort はまず次の順で決まる:

1. `(effort: VALUE)` — source `step-effort`。
2. `(workflow-step: ID)` はちょうど1つの一致する active workflow 行だけを選ぶ。重複 ID は block する。pin された
   `basic`、`pro`、`ultra` の cell は source `workflow-effort` を使い、`(infer)` は
   最終 instruction の分類に委ね source `inferred-effort` になる。
3. 明示 effort、bound pin のいずれも無い場合、orchestrator は最終 instruction を
   `basic`、`pro`、`ultra` に分類する。不明なら `pro` を使う。

model 要求は別に採用される。優先順位は: 実行時プロンプト（run 全体・ステップ単位・
effort 単位の指定 — ステップの `(model: VALUE)` も、今回の実行対象として指定した
入力文書から正規化された実行時要求である）→ 実行環境の子エージェント用モデル設定
（`absent` / `unset` のときだけ下位へ落ちる。`invalid` や観測不能は黙って置き換えず
run を停止する）→ skill 既定の profile（要求がステップ metadata 由来なら source
`step-model`）。解決済み model があるターンの effort は `null` になる。文書内の引用、
過去 run の記録、成果物での言及は要求ではない。plan はターンごとに、model 要求の出典と
元の文言・適用範囲、解決先を見せる。

workflow 行が sequence position、mode の一致、semantic similarity で自動的に bind されることは
ない。挿入される debug と llm-decision ターンだけが、policy で選ばれた `ultra` effort を
source `policy-effort` として使う。profile、mapping、pin の欠落または不正は `blocked` で停止し、
harness default や effort 間 fallback は無く、壊れた profile を上位指定が隠すこともない。

`--auto` なしで応答者がいない run は、plan の提示で終わる。

`--auto` の下では、使われる全 model 解決が指定内容との exact match である間だけ、
plan はターンごとの確認なしで走る。実行時に自然言語・略称・別名・接頭辞・系列から
新たに解決した model は、適用範囲と解決先の明示的承認を常に必要とする — headless なら
run は開始前に停止し、解決内容を見せる。

## Failure リカバリループ

`execute` ターンが planned check（例: テスト）を走らせて失敗し、その失敗がリポジトリ内で修正可能に見える場合、status `failed` を返し **Failure report**（Error / Reproduction / Error output / Target file(s) / Context）を書く。オーケストレータは次に:

1. `debug` ターンを挿入 — 根本原因を診断し最小 diff を提案する（diff の適用は自身では行わない）、
2. re-execute ターンを挿入 — その diff を適用し check を再実行する、
3. 通れば本流に復帰、通らなければもう1サイクル回す。

ターンごとの**サイクル上限**はデフォルト 2（workflow spec で上書き可）。上限に達してもなお失敗している場合は `blocked` に格上げして run を停止する。`debug` ターンが `needs-human`（例: 修正がタスクの許可範囲外）を返した場合も run を停止する。

### リカバリループに入らないもの

診断する価値があるのは `failed`（作業は走った・check が通らなかった・ここで直せそう）だけ。次の4つは意図的にループを迂回する:

- **`blocked`** — Pi の CLI 経路では、プロンプトファイルの書込失敗・読込失敗・空内容も CLI 起動前に停止する。再試行せず、abort の再投入ゲートにもリカバリにも入れない。run index にはパスと診断を記録し、実行されていない委譲の attempt は作らない。ツール呼び出しが**権限システムに拒否**されたターンも含む。拒否はリポジトリ内のバグではないので、再実行しても毎サイクル同じ壁に当たる。run を止めて利用者に尋ねる。ターン自身がこれを免除することはできない — 拒否された呼び出しを「不可欠でない」と判断し、それ無しでタスクを完了できた場合でも、status は `ok` ではなく必ず `blocked` になる。拒否された呼び出しの効果を別の手段で再現してもならない（拒否された `git stash` の代わりに `cp` や編集でファイルを戻すなど）。別の、より安全な操作は再現に当たらない。Claude Code ではこれを機械検査も行う: 各ターンの status 行を読んだ後に `scripts/deny_scan.sh` がターンと、そのターンが起動した agent の transcript を走査し、検出された拒否は自己申告の `ok` を上書きする（orchestrator 自身が停止したターンでは、代わりに `needs-human` になる: 停止が拒否に見える痕跡を残すことがあるため）。検査は拒否された呼び出しをすべて数える — 権限ルール、plugin の hook、auto mode classifier（到達できなかった場合を含む）、承認されなかった許可確認、中断された呼び出し。判定結果は run ディレクトリに試行ごとのファイルとして残る（ターン `05` の 1 回目の試行なら `05-1-deny.txt`）。
- **`needs-human`** — 利用者にしか提供できない具体的な情報・判断・操作が必要なターン。
  返答には `Reason: <specific observed blocker>` と
  `Human request: <exact question to answer or action to take>` を最終 status 行の前に含め、
  理由や依頼を知るために成果物を読ませない。単に複雑・未完了というだけでは利用者への依頼にならない。
- **`needs-decision`** — 失敗ではなく分岐に当たったターン。後述の decision ループへ回る。
- **`aborted`** — タスクについて**何も報告していない**ターン。返答に所定の status 行が無い（中断・kill・契約逸脱）場合、**形式は正しい status 行が最終行以外に置かれている**場合（アンカーは位置であり、位置を外して読むと途中で切れた返答が完了扱いで通ってしまう）、そしてそもそも返答が来ず**ターン watchdog**（後述）が打ち切った場合の両方を含む。診断すべき失敗が存在しないので、orchestrator は診断しない。そのターンは**最大1回だけ**、**再投入ゲート**を通ったときに限って再実行する: 停止が確認され、拒否検査（Claude Code）が clean で、判定役（`ultra` の固定の委譲で、ターンではない）が、その試行の証拠から「ターンを最初からやり直しても、既に起きた作用を重複させない」と判断すること。そうでなければ、最初の abort で `needs-human` として run を停止する。再実行はサイクル上限を消費しない。

各ターンは返答の**最終行**を `status: <...>; file: <path>` に固定し、orchestrator はその行だけから結果を読む。だから status 行を欠いたターンは推測せず `aborted` として扱う — 沈黙した／壊れたターンが成功として通り抜けるのではなく、はっきり失敗する。

**`failed` を提示されるのは `execute` ターンだけ。** この status は「planned check が通らなかった」と定義されており、その check を走らせるのは `execute` ターンだから。他のモードには `ok` / `blocked` / `needs-human` / `needs-decision` を渡す。それでも他のターンが `failed` を返した場合は契約違反として扱い、リカバリループ（診断すべき Failure report が存在しない）には入れず `needs-human` で run を停止する。

## Decision ループ

ターンは失敗ではなく**分岐**で止まることもある — 自分では解決してはいけない曖昧さ、優劣の付かない2つのアーキテクチャ、推奨を1つに絞れないレビュー所見。その受け皿が無ければ、そうしたターンの出口は `needs-human` しかなく、run 全体が終わる。

このときターンは status `needs-decision` を返し、deliverable に **Decision request** を4フィールドで書く:

| フィールド | 内容 |
|---|---|
| Question | 何を決めるのか。分岐の各側がどこから来たかを名指す: ターンの `Task:` 行（step の文面そのもの）、`Part:` 行（分割された step のうちそのターンの担当分を orchestrator が書いた文）、入力のパス、または常設ルール。 |
| Options | 2つ以上、それぞれの trade-off 付き。 |
| Impact on remaining steps | `none`、または改訂後の残ステップ列。 |
| Work state | `complete`（deliverable は有効で、分岐の申告のみ）または `stopped`（決定なしでは完了できなかった）。 |

不備のある request は decision として扱わない — `needs-human` として読み、run を停止する。これは意図的で、この4フィールドこそが「本物の分岐」と「仕事の丸投げ」を分けている。

その先は `--decider` によって変わる:

- **`llm`** — 裁定用に `review-dev` ターンを1つ挿入する。フォークを申告したターンの deliverable（それが挿入された debug ターンならその deliverable）、plan があればそれ、そして**入力ドキュメント**（分岐を「何に向けて」決めるかは run の目的であり、それは入力ドキュメントにしか無い）を受け取り、ちょうど1つの選択肢を理由付きで記録する。request に列挙されていない選択肢を採ってもよい（その旨を明記する）。ただし不可逆・外向きの作用を伴う決定、run の目的自体を変える決定は**裁定してはならない** — それらは `needs-human` として利用者に戻る。
- **`human`（デフォルト）** — orchestrator はまず run index で、Question が名指した各側の出所を確かめる。すべてがこの run の中でモデルが書いた指示（他のターンの deliverable、または分割 step の `Part:` 行）なら、その矛盾をあなたは知りようがないので、`llm` と同じく `review-dev` ターンを挿入して裁定させる（後述の decision 上限にも数える）。それ以外はターンを挿入しない。run はステップ境界で止まり、質問と deliverable のパスを提示する。あなたの回答が decision record に書かれ、run が続く。挿入されたターンが裁定してはならないと判断した場合（片側が人の書いたものに依る、または上記の条件に当たる）も、同じように提示される。非対話実行（`claude -p` など）では提示された分岐に回答する主体が居ないので run はそのまま終わる — request はディスク上に残るので、それを読んで再開できる。

続行は2形態のいずれかで、`Work state` フィールドから読み取る（推測しない）: **`complete`** かつ列挙内の選択肢 → deliverable は有効なまま、decision evidence を後続ターンの inputs に追加する。**`stopped`** または列挙外の選択肢の採用 → フォークを申告したターンを再実行する。親は、decision turn が裁定したならそのターン、あなたが裁定したなら human decision record であり、いずれも raising turn の model routing を保持する。

決定が **amendment** を伴う場合、再生成されるのは**未実行部分の turn plan だけ**（完了済みターンは不変）で、改訂後の残りは改めて Step 0 ゲートを通す。元の plan は run index に残るので、承認した内容からの drift は常に見える。

ターンごとの **decision 上限**は 2 回の挿入（workflow spec で上書き可）。数えるのは**挿入されたターン**で、`--decider=llm` ではすべてのフォーク、`human` では挿入ターンに回されたフォークが対象になる — あなたが裁定するフォークは何も挿入せず、上限も存在しない（同じターンが3つ目・4つ目のフォークを持ってきても、そのつど提示される）。上限の目的は無人の run が堂々巡りで裁定し続けるのを止めることであり、毎回あなたの回答を要する run はそもそも堂々巡りできない。ここに上限を置いても安全は増えず、正当な3つ目のフォークが「上限到達」で返ってくるだけになる。上限に達したら `blocked` ではなく `needs-human` で停止する — 2回裁定しても収束しないのは判断過負荷であり、欠けているのは能力ではなくあなたの判断だから。decision 上限とリカバリのサイクル上限は**別々に**数え、どちらも接尾辞列を所有する起点ターンが持つ。**その列に加わるターンは、再実行であれ挿入された debug ターンであれ挿入された decision ターンであれ、すべて起点の残量から使う**（自分用の上限を新たに持たない）。そうでないと、ターンを1つ挿入するだけで毎回新しい上限が湧いてしまう。

フォークは**挿入されたターン**が申告することもある（例: debug ターンが修正案を複数見つけた場合）。そのとき続行形態が (b) なら、再実行されるのはフォークを申告したターン（= debug ターン）であって元のターンではない。そしてその再実行はリカバリのサイクルを消費しない — リカバリ上限が数えるのは failed のサイクルであり、フォークは失敗ではないから。この往復が消費するものがあるとすれば decision の挿入1回だけで、それは `--decider=llm` のとき、または `human` で挿入ターンに回されたときに限られる。

`--auto` は `--decider=human` を上書き**しない**。`--auto` が飛ばすのは初回の turn plan 承認だけで、human 裁定の run は分岐で待つ。

権限拒否は decision ではない。拒否されたターンが `needs-decision` を報告した場合、`ok` の誤報告を捕まえるのと同じ機械検査で `blocked` に是正される — さもないと壁が decision 1回分を消費するか、最悪の場合は裁定で壁を迂回されてしまう。

## ターン watchdog

status 行が分類できるのは**返答してきたターンだけ**。subagent は返答自体をやめることもある — kill・中断・単なるハング — この場合は通知すら来ない。orchestrator はそれを自力で気づけない: 何かに起こされたときにしか動かないので、来ない返答を待つことは終わらない待機になる。

そこで各ターンは、背景で走る **watchdog**（`scripts/watchdog.sh`）と一緒に起動する。watchdog が orchestrator を起こすのは異常が起きたときだけで、次のうち早い方を報告して終了し、**その終了自体が orchestrator を起こす**。watchdog が起動できない（コマンドが拒否される、またはすぐ終了する）場合、orchestrator はそのターンをただちに停止し、run を `blocked` で終える: 時間の上限が無いターンを走らせたままにはしない。起床は次の 2 つ:

- **ウォールクロック期限**を超過 — `TIMEOUT`;
- subagent の transcript が停滞閾値のあいだ**伸びなくなる** — `STALL`。

正常系はこのどちらでもない。完了したターンは自身の完了通知を送り、orchestrator はその時点で watchdog タスクを停止する。watchdog は成果物が現れても**意図的に exit しない**: subagent はファイルを書いてから返答を作文するので、そこで抜けるとターン自身の通知より先に orchestrator を起こしてしまい、しかも返答作文の末尾区間が完全に無時限になる——watchdog が防ぐために存在している「検出されない待機」そのもの。ファイルは引き続き監視しており、`TIMEOUT` と `STALL` のどちらのメッセージも成果物が書かれていたかどうかを述べる。末尾ハングが読み取れるのはこのおかげ: 成果物はあるのに transcript が静止した、という形で現れる。

このメッセージにおいて「そのターンの実行中に書かれた」は効いている条件。監視する path に既にあるファイルは、そのままではこのターンの出力に見えてしまう（再実行は自分の path `<turn key>-<mode>-attempt<n>.md` に書くが、この検査はそれに頼らない）。そこで watchdog は、自身の起動時刻より新しい成果物だけを数える。古いものは stale として trace に1回だけ記録し、そのターンの出力としては決して報告しない。

watchdog は報告するだけで、自分では何も停止しない。`TIMEOUT` / `STALL` を受けると orchestrator はまず、形式の正しい返答が既に手元にあるかを確かめる（あればその返答でターンを分類する）。無ければターンを停止し、停止を最大 60 秒観測して `aborted` に分類する。再実行は再投入ゲートが許したときだけ（上の `aborted` を参照）— 読めない返答とまったく同じ扱い。失敗の診断はしない。何も報告しなかったターンには診断する材料が無いため。

既定値はスクリプト冒頭にある。変更はそこで行う:

| 設定 | 既定値 |
|---|---|
| 停滞閾値 | 600s |
| 期限 `survey` | 7200s |
| 期限 `plan` | 4800s |
| 期限 `execute` | 4800s |
| 期限 `review-dev` | 3600s |
| 期限 その他のモード | 1800s |
| ポーリング間隔 | 15s |

意図的に余裕を持たせてある。長いツール呼び出しは外から見るとハングと区別がつかない（どちらも transcript は静止する）ので、停滞閾値はターンが行いうる最長のツール呼び出しより十分大きく取る。これは「本物のハングの検出が遅れる」代わりに「作業中のターンを切らない」側に倒す判断。まだ進行中だったターンが繰り返し abort されるなら予算が厳しすぎる。どの検査が発火したかは run index に記録されるので、それを見て引き上げられる。これは最初の試行の予算で、再実行は判定役の `ALLOW` 行の秒数で動く。`TIMEOUT` の後では、その秒数は試行が到達した期限より長く、試行自身の transcript の 1 行で裏づけられている。

watchdog が縛るのは時間であって正しさではない。「ターンが長すぎた」ことは言えるが、「ターンが間違ったことをした」ことは言えない。

## Workflow spec

workflow spec はタスク種別のガイダンスを供給する。推奨列の columns は安定した
`id`、`mode`、`effort`、`task` であり、これに failure-policy cap が加わる。effort cell は
`(infer)` または `basic`、`pro`、`ultra` のいずれかで、model を名指しせず、mode に対する
一般的な effort default も定めない。

pin が適用されるのは、番号付き todolist ステップが明示的に `(workflow-step: ID)` を持つ場合だけ。
bound `(infer)` 行も最終 instruction の分類を使う。この metadata が無ければ全 workflow 行は
ガイダンスまたは警告に留まり、ターンは自身の model または effort metadata が勝たない限り
inferred effort を使う。明示 model と effort は常に binding より優先する。

本スキルは spec を 2 つ同梱する。`--workflow=<name>` または todolist 内での名指しで有効化する。別のタスク種別の spec を追加するには `WORKFLOW_SPEC_AUTHORING.md` を参照。

- **`dev`**（`workflows/dev.md`）: 開発／実装作業向け（調査 → 設計 → レビュー → 実装 → クラス名指し → テスト → レビュー → docs 同期）。
- **`research`**（`workflows/research.md`）: 範囲の定まった問いに対する証拠ベースの調査向け（method → gather → validate → reason → draft → challenge。1 回目で見つかった欠落を埋める 2 回目は任意）。ターン向けの証拠・範囲の方針も持つが、それがターンに届くのは todolist が spec ファイルをそのターンの入力として渡したときだけ。

## run ディレクトリと成果物

起動ごとに `<ws>/tmp/runs/<YYYY-MM-DD>_<slug>/` に run ディレクトリを1つ作る（`<ws>` はセッションを起動したディレクトリで、worktree の中で起動していてもそれ）。同名がすでにあれば再利用せず接尾辞を足す。run がこのディレクトリを削除することはない。中身:

- `NN-<mode>.md` — ターンごとの deliverable を順に。起点ターン `NN` に対して挿入されたターンは、どちらのループのものでも次の空き接尾辞を取る。両方を通ったターンは `05a-decision.md` → `05b-execute.md` → `05c-debug.md` → `05d-execute.md` と読める。ファイル名に mode が入るので、どのループの成果物かは常に一意。abort の後の再実行は `<turn key>-<mode>-attempt<n>.md`（`05-execute-attempt2.md`）に書き、前の試行のファイルは残る。
- 試行ごとの 1 組のファイル。名前は `<turn key>-<attempt>-…`（`05-1-deny.txt`、`05-1-stop.json`、`05-1-evidence.json`、`05-1-judge-prompt.md`、`05-1-judge.md`。Pi の CLI 経路ではさらに `05-1-pids.json`、`raw/`、`sessions/`）。Pi CLI に渡すメッセージは、短い shell 呼出しの前に書込ツールで UTF-8 の `<turn key>-<attempt>-prompt.md` へ保存し、試行ごとに別ファイルとして残す。judge の短い起動メッセージは `<turn key>-<attempt>-judge-launch-prompt.md` とし、judge gate が生成する指示本文 `…-judge-prompt.md` と区別する。これらも run に保持し、長文の shell heredoc では作らない。
- `index.md` — canonical run index。人間が読む plan と、機械可読な JSONL block をちょうど1つ含む。入れ子の、または prose だけの `index.md` は run index ではない。delegation 前にこの block は contract と初期 plan record を書く:

  ```json
  {"record":"contract","version":"mo-delegation-v2"}
  {"record":"plan","id":"r0","replaces":null,"after_turn":null,"approval_ref":"ap1","contingency_policy_ref":"cp1"}
  ```

  初期ターンの `"kind":"planned"` は常にこのliteralであり、modeは別に記録してkindにはしない。続けて次のような planned turn definition を書く — selection に model 要求の出源と文言・適用範囲、解決先を記録する:

  ```json
  {"record":"turn","key":"03","plan":"r0","kind":"planned","step":2,"parent":null,"inherits":null,"inputs":[],"mode":"plan","effort":"ultra","effort_source":"inferred-effort","budget_refs":{"recovery_owner":"03","decision_owner":"03","restart_owner":"03"},"rebinds":null,"amendment_ref":null,"selection":{"source":"skill","request":"(default profile)","scope_ref":"input doc step 2","resolution_ref":"res-03","resolved_model":"opus","granularity":"harness-ref","thinking":{"state":"undisclosed"}},"dispatch_plan":{"route":"native-subagent","args":{"model":"opus"}}}
  ```

  delegation call が完了または abort した**後だけ**、実際の call evidence を追記する — delegation record は call が実際に渡したものを運ぶ:

  ```json
  {"record":"delegation","id":"d1","owner":"03","owner_kind":"turn","plan":"r0","approval_ref":"ap1","resolution_ref":"res-03","policy_ref":"perm1","route":"native-subagent","actual_selection_args":{"model":"opus"},"call_ref":"raw/03-1.jsonl","reported_model":"opus","reported_thinking":null}
  {"record":"attempt","turn":"03","attempt":1,"delegation_id":"d1","reported_status":"ok","effective_status":"ok","file":"03-plan.md"}
  ```

  `dispatch_plan` / `actual_selection_args` は model / effort / 固定解決参照のちょうど1つを運び、actual は call が実際に渡したものであり plan からの後付けではない。解決 model は tier ではない。call 前に停止した run（経路の利用不能、解決変動）は delegation も attempt も残さず、call しても開始しなかった場合は delegation に null の報告を残し、attempt の effective status が分類（`blocked`、`stale-plan`、`aborted`）を運ぶ。分割された1ステップは、同じ番号付き `step` を持つ複数の planned turn key を持ちうる。aborted retry は同じ turn に次の attempt を追加する。index は status、decision 挿入、amendment、human decision wait、aborted retry を生じさせた check も記録し、解決・許可・承認の詳細は run ディレクトリの追記専用 `approvals.jsonl` / `resolutions.jsonl` / `policy.jsonl` に置かれ `*_ref` で参照される。動的挿入は plan の承認済み contingency policy に従い、それに紐付く `derived` 承認を運び、その delegation は当該 derived 承認を `approval_ref` に持つ。
  `scripts/delegation_plan.js validate` / `dispatch-check` / `audit` が台帳の整合性を検査する — データの整合であって証跡の真正ではない。run の3つの証跡ファイルを渡して実行する。証跡が欠ける規則は黙ってskipではなく違反になる。
  検査用インデックスであり、再開可能なスケジューラではない。以前の `adaptive-effort-v1` の run は読取り専用で保持される。

これらはランタイム成果物 — コミットしない。

## しないこと

- `execute` ターン周りの rollback / checkpoint / worktree は無い — 作業ツリーの安全はあなたの git 衛生の責務。autonomous な run の前に WIP を commit / stash すること。このスキルは worktree を用意しない。run が作った worktree はその run のものとして保持され、削除は保留と報告される。run が完了した後（またはあなたが run を破棄したとき）で、かつ別の削除ルール（`run_owned_worktree_removal`）が有効なときに限って削除される。
- **ステップ途中**の中断や再開スケジューラは無い。`--decider=human` の待機はこれの例外ではない — ステップ境界で起きるので、その時点で全成果物はすでに書かれている。
- 並列ターンは無い — ターンは順番に実行される。
- ゼロ分解は無い — todolist は入力に存在している必要がある。

change-report が「どのファイルを編集したか」と主張する内容ではなく、run の**最終状態**（planned check の再実行）を信頼すること。change-report は自己申告である。

## 関連ドキュメント

- LLM 向け仕様: `SKILL.md`（`skills/mode-orchestrator/` 配下）。
- workflow spec の書き方: 同じフォルダの `WORKFLOW_SPEC_AUTHORING.md`。
- 同梱の spec: `skills/mode-orchestrator/workflows/dev.md` と `skills/mode-orchestrator/workflows/research.md`。
