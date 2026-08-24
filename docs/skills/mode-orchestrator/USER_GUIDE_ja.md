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

**headless で起動する場合の前提が 1 つある**（`claude -p`、ラッパースクリプト、入れ子 run）。ゲートの前に、スキルはどのハーネス上に居るかを判定する小さなシェルコマンドを 1 回実行する。Claude Code ではこのコマンドに許可が要り、対話セッションならその場で承認できるが、**headless は尋ねる相手が居ないため拒否され、run はそこで止まる**。その run が読む設定でこの 1 コマンドを許可するか、`--allowedTools` で渡すこと。正確な文字列は `references/harness-cc.md` にある。通常の対話実行では何もしなくてよい。

## 起動 — フラグ

| フラグ | 効果 |
|---|---|
| _(なし)_ | turn plan を提示し、実行前に承認を待つ。 |
| `--auto` | 承認ゲートを飛ばし、全ターンを承認なしで実行する。 |
| `--roles` / `--roles=always` | 各ターンに適合する role を推論して付与する。デフォルト: role は推論しないが、todolist に明示された role は honor する。 |
| `--workflow=<name>` | workflow spec（`workflows/<name>.md`）を defaults として読み込む。todolist 内で spec 名を宣言しても同様に honor される。デフォルト: spec なし — todolist の指定どおりに実行。 |
| `--decider=llm` | `needs-decision` の分岐を挿入されたターンに裁定させる。デフォルトは `human` — run はステップ境界で止まり、質問を提示する。後述の decision ループを参照。 |

フラグは意図的に `--` 形式を使う。`mode:` / `role:` のコロン接頭辞は使わない（role-mode フックに捕捉されるため）。

## Mode

**Autonomous**（subagent ターンとして実行）: `survey`, `plan`, `execute`, `debug`, `review`, `review-dev`。エイリアス: `verify` → `debug`, `implement` → `execute`。

**Interactive**（実行しない — ネイティブ実行の提案として提示）: `ask`, `discuss`, `brainstorm`, `organize`。これらは autonomous subagent には提供できない、人間とのライブなやり取りを必要とする。

mode は hybrid で決定: ステップが mode を名指ししていれば honor、なければステップ内容から適合 mode を推論する。

## ターンごとの model

各ターンは特定の model で実行できる。model は優先順位で解決され、mode 単独からの推測はしない:

1. **ステップ明示** — todolist のステップで名指しされた model（または有効な workflow spec がそのステップに pin した model）。衝突時は todolist が勝つ。
2. **spec 表** — 有効な workflow spec の mode→model デフォルト。
3. **継承** — そのターンは自前の model を持たないので、override 無しの委譲を
   harness が何で走らせるかに従う。Claude Code ではセッションの model。Pi では
   **pi 自身の設定 default で、Claude 系とは限らない**（実測）。Pi で特定の
   model が要るなら継承に頼らず、ステップか spec 表で pin すること。

run index には「どの段が決めたか」と「実際に渡した override（無ければ `none`）」
の両方を記録する。後者が無いと、index が `inherit` と主張しながら呼び出しでは
model を指定していた、という乖離が起きても後から検出できない。

turn plan は各ターンの model と、どの段が決定したかを表示する。

## Failure リカバリループ

`execute` ターンが planned check（例: テスト）を走らせて失敗し、その失敗がリポジトリ内で修正可能に見える場合、status `failed` を返し **Failure report**（Error / Reproduction / Error output / Target file(s) / Context）を書く。オーケストレータは次に:

1. `debug` ターンを挿入 — 根本原因を診断し最小 diff を提案する（diff の適用は自身では行わない）、
2. re-execute ターンを挿入 — その diff を適用し check を再実行する、
3. 通れば本流に復帰、通らなければもう1サイクル回す。

ターンごとの**サイクル上限**はデフォルト 2（workflow spec で上書き可）。上限に達してもなお失敗している場合は `blocked` に格上げして run を停止する。`debug` ターンが `needs-human`（例: 修正がタスクの許可範囲外）を返した場合も run を停止する。

### リカバリループに入らないもの

診断する価値があるのは `failed`（作業は走った・check が通らなかった・ここで直せそう）だけ。次の4つは意図的にループを迂回する:

- **`blocked`** — ツール呼び出しが**権限システムに拒否**されたターンを含む。拒否はリポジトリ内のバグではないので、再実行しても毎サイクル同じ壁に当たる。run を止めて利用者に尋ねる。ターン自身がこれを免除することはできない — 拒否された呼び出しを「不可欠でない」と判断して回避しタスクを完了できた場合でも、status は `ok` ではなく必ず `blocked` になる。Claude Code ではこれを機械検査も行う: 各ターンの status 行を読んだ後に `scripts/deny_scan.sh` が transcript を走査し、検出された拒否は自己申告の `ok` を上書きする。
- **`needs-human`** — 利用者にしか下せない判断が必要なターン。
- **`needs-decision`** — 失敗ではなく分岐に当たったターン。後述の decision ループへ回る。
- **`aborted`** — タスクについて**何も報告していない**ターン。返答に所定の status 行が無い（中断・kill・契約逸脱）場合、**形式は正しい status 行が最終行以外に置かれている**場合（アンカーは位置であり、位置を外して読むと途中で切れた返答が完了扱いで通ってしまう）、そしてそもそも返答が来ず**ターン watchdog**（後述）が打ち切った場合の両方を含む。診断すべき失敗が存在しないので、orchestrator はそのターンを**1回だけ**再実行し、それでも読めなければ `needs-human` で停止する。この再実行はサイクル上限を消費しない。

各ターンは返答の**最終行**を `status: <...>; file: <path>` に固定し、orchestrator はその行だけから結果を読む。だから status 行を欠いたターンは推測せず `aborted` として扱う — 沈黙した／壊れたターンが成功として通り抜けるのではなく、はっきり失敗する。

**`failed` を提示されるのは `execute` ターンだけ。** この status は「planned check が通らなかった」と定義されており、その check を走らせるのは `execute` ターンだから。他のモードには `ok` / `blocked` / `needs-human` / `needs-decision` を渡す。それでも他のターンが `failed` を返した場合は契約違反として扱い、リカバリループ（診断すべき Failure report が存在しない）には入れず `needs-human` で run を停止する。

## Decision ループ

ターンは失敗ではなく**分岐**で止まることもある — 自分では解決してはいけない曖昧さ、優劣の付かない2つのアーキテクチャ、推奨を1つに絞れないレビュー所見。その受け皿が無ければ、そうしたターンの出口は `needs-human` しかなく、run 全体が終わる。

このときターンは status `needs-decision` を返し、deliverable に **Decision request** を4フィールドで書く:

| フィールド | 内容 |
|---|---|
| Question | 何を決めるのか、1文で。 |
| Options | 2つ以上、それぞれの trade-off 付き。 |
| Impact on remaining steps | `none`、または改訂後の残ステップ列。 |
| Work state | `complete`（deliverable は有効で、分岐の申告のみ）または `stopped`（決定なしでは完了できなかった）。 |

不備のある request は decision として扱わない — `needs-human` として読み、run を停止する。これは意図的で、この4フィールドこそが「本物の分岐」と「仕事の丸投げ」を分けている。

その先は `--decider` によって変わる:

- **`llm`** — 裁定用に `review-dev` ターンを1つ挿入する。フォークを申告したターンの deliverable（それが挿入された debug ターンならその deliverable）、plan があればそれ、そして**入力ドキュメント**（分岐を「何に向けて」決めるかは run の目的であり、それは入力ドキュメントにしか無い）を受け取り、ちょうど1つの選択肢を理由付きで記録する。request に列挙されていない選択肢を採ってもよい（その旨を明記する）。ただし不可逆・外向きの作用を伴う決定、run の目的自体を変える決定は**裁定してはならない** — それらは `needs-human` として利用者に戻る。
- **`human`（デフォルト）** — ターンは挿入しない。run はステップ境界で止まり、質問と deliverable のパスを提示する。あなたの回答が decision record に書かれ、run が続く。非対話実行（`claude -p` など）では回答する主体が居ないので run はそのまま終わる — request はディスク上に残るので、それを読んで再開できる。

続行は2形態のいずれかで、`Work state` フィールドから読み取る（推測しない）: **`complete`** かつ列挙内の選択肢 → deliverable は有効なまま、decision record を後続ターンの inputs に追加する。**`stopped`** または列挙外の選択肢の採用 → 起点ターンを decision record 付きで再実行する。

決定が **amendment** を伴う場合、再生成されるのは**未実行部分の turn plan だけ**（完了済みターンは不変）で、改訂後の残りは改めて Step 0 ゲートを通す。元の plan は run index に残るので、承認した内容からの drift は常に見える。

ターンごとの **decision 上限**は 2 回の挿入（workflow spec で上書き可）。数えるのは**挿入されたターン**なので、効くのは `--decider=llm` の run だけ — あなたが裁定する場合は何も挿入されず、上限も存在しない（同じターンが3つ目・4つ目のフォークを持ってきても、そのつど提示される）。上限の目的は無人の run が堂々巡りで裁定し続けるのを止めることであり、毎回あなたの回答を要する run はそもそも堂々巡りできない。ここに上限を置いても安全は増えず、正当な3つ目のフォークが「上限到達」で返ってくるだけになる。上限に達したら `blocked` ではなく `needs-human` で停止する — 2回裁定しても収束しないのは判断過負荷であり、欠けているのは能力ではなくあなたの判断だから。decision 上限とリカバリのサイクル上限は**別々に**数え、どちらも接尾辞列を所有する起点ターンが持つ。**その列に加わるターンは、再実行であれ挿入された debug ターンであれ挿入された decision ターンであれ、すべて起点の残量から使う**（自分用の上限を新たに持たない）。そうでないと、ターンを1つ挿入するだけで毎回新しい上限が湧いてしまう。

フォークは**挿入されたターン**が申告することもある（例: debug ターンが修正案を複数見つけた場合）。そのとき続行形態が (b) なら、再実行されるのはフォークを申告したターン（= debug ターン）であって元のターンではない。そしてその再実行はリカバリのサイクルを消費しない — リカバリ上限が数えるのは failed のサイクルであり、フォークは失敗ではないから。この往復が消費するものがあるとすれば decision の挿入1回だけで、それも `--decider=llm` のときに限られる。

`--auto` は `--decider=human` を上書き**しない**。`--auto` が飛ばすのは初回の turn plan 承認だけで、human 裁定の run は分岐で待つ。

権限拒否は decision ではない。拒否されたターンが `needs-decision` を報告した場合、`ok` の誤報告を捕まえるのと同じ機械検査で `blocked` に是正される — さもないと壁が decision 1回分を消費するか、最悪の場合は裁定で壁を迂回されてしまう。

## ターン watchdog

status 行が分類できるのは**返答してきたターンだけ**。subagent は返答自体をやめることもある — kill・中断・単なるハング — この場合は通知すら来ない。orchestrator はそれを自力で気づけない: 何かに起こされたときにしか動かないので、来ない返答を待つことは終わらない待機になる。

そこで各ターンは、背景で走る **watchdog**（`scripts/watchdog.sh`）と一緒に起動する。watchdog が orchestrator を起こすのは異常が起きたときだけで、次のうち早い方を報告して終了し、**その終了自体が orchestrator を起こす**:

- **ウォールクロック期限**を超過 — `TIMEOUT`;
- subagent の transcript が停滞閾値のあいだ**伸びなくなる** — `STALL`。

正常系はこのどちらでもない。完了したターンは自身の完了通知を送り、orchestrator はその時点で watchdog タスクを停止する。watchdog は成果物が現れても**意図的に exit しない**: subagent はファイルを書いてから返答を作文するので、そこで抜けるとターン自身の通知より先に orchestrator を起こしてしまい、しかも返答作文の末尾区間が完全に無時限になる——watchdog が防ぐために存在している「検出されない待機」そのもの。ファイルは引き続き監視しており、`TIMEOUT` と `STALL` のどちらのメッセージも成果物が書かれていたかどうかを述べる。末尾ハングが読み取れるのはこのおかげ: 成果物はあるのに transcript が静止した、という形で現れる。

このメッセージにおいて「そのターンの実行中に書かれた」は効いている条件。再実行は置き換える対象と同じパスへ書くので、2回目の開始時点で既にファイルが存在することが多い。そこで watchdog は、自身の起動時刻より新しい成果物だけを数える。古いものは stale として trace に1回だけ記録し、そのターンの出力としては決して報告しない。

watchdog は報告するだけで、自分では何も停止しない。`TIMEOUT` / `STALL` を受けてターンを停止し `aborted` に分類して1回だけ再実行するのは orchestrator の側 — 読めない返答とまったく同じ扱い。診断はしない。何も報告しなかったターンには診断する材料が無いため。

既定値はスクリプト冒頭にある。変更はそこで行う:

| 設定 | 既定値 |
|---|---|
| 停滞閾値 | 600s |
| 期限 `survey` | 600s |
| 期限 `plan` | 2400s |
| 期限 `execute` | 1500s |
| 期限 その他のモード | 900s |
| ポーリング間隔 | 15s |

意図的に余裕を持たせてある。長いツール呼び出しは外から見るとハングと区別がつかない（どちらも transcript は静止する）ので、停滞閾値はターンが行いうる最長のツール呼び出しより十分大きく取る。これは「本物のハングの検出が遅れる」代わりに「作業中のターンを切らない」側に倒す判断。まだ進行中だったターンが繰り返し abort されるなら予算が厳しすぎる。どの検査が発火したかは run index に記録されるので、それを見て引き上げられる。

watchdog が縛るのは時間であって正しさではない。「ターンが長すぎた」ことは言えるが、「ターンが間違ったことをした」ことは言えない。

## Workflow spec

workflow spec は1つのタスク種別に対する **defaults とガイダンス**（推奨ステップ列・mode→model 表・failure policy の上限）を、エンジンを変えずに供給する。spec は弱結合: todolist が常に正であり、todolist と spec の不整合は**警告**として表れるだけでリジェクトにはならない。

本スキルは開発／実装作業向けの spec **`dev`**（`workflows/dev.md`）を同梱する（調査 → 設計 → レビュー → 実装 → クラス名指し → テスト → レビュー → docs 同期）。`--workflow=dev` または todolist 内での名指しで有効化する。別のタスク種別の spec を追加するには `WORKFLOW_SPEC_AUTHORING.md` を参照。

## run ディレクトリと成果物

起動ごとに workspace に run ディレクトリを1つ作る（例: `mode-orchestrator-runs/<run-slug>/`）:

- `NN-<mode>.md` — ターンごとの deliverable を順に。起点ターン `NN` に対して挿入されたターンは、どちらのループのものでも次の空き接尾辞を取る。両方を通ったターンは `05a-decision.md` → `05b-execute.md` → `05c-debug.md` → `05d-execute.md` と読める。ファイル名に mode が入るので、どのループの成果物かは常に一意。
- `index.md` — turn plan（各ターンの model: 決定した段と、実際に渡した override（無ければ `none`）の両方）、spec 警告、Failure & decision policy、各ターンの status。加えて、各ターンの decision 挿入回数と採った続行形態、amendment（差し替えた元の plan も併記）、`--decider=human` の待機、`aborted` を受けて再実行したターンとどの検査が捕まえたかも記録する — aborted ターンは deliverable を書かないため、それが起きた事実を残す唯一の場所になる。検査用インデックスであり、再開可能なスケジューラではない。

これらはランタイム成果物 — コミットしない。

## しないこと

- `execute` ターン周りの rollback / checkpoint / worktree は無い — 作業ツリーの安全はあなたの git 衛生の責務。autonomous な run の前に WIP を commit / stash すること。
- **ステップ途中**の中断や再開スケジューラは無い。`--decider=human` の待機はこれの例外ではない — ステップ境界で起きるので、その時点で全成果物はすでに書かれている。
- 並列ターンは無い — ターンは順番に実行される。
- ゼロ分解は無い — todolist は入力に存在している必要がある。

change-report が「どのファイルを編集したか」と主張する内容ではなく、run の**最終状態**（planned check の再実行）を信頼すること。change-report は自己申告である。

## 関連ドキュメント

- LLM 向け仕様: `SKILL.md`（`skills/mode-orchestrator/` 配下）。
- workflow spec の書き方: 同じフォルダの `WORKFLOW_SPEC_AUTHORING.md`。
- 同梱の `dev` spec: `skills/mode-orchestrator/workflows/dev.md`。
