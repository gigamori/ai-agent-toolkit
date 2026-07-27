# mode-orchestrator — ユーザーガイド

`mode-orchestrator` スキルの利用者向けガイド。**todolist**（指示のリスト）と各ステップに必要な context をすでに含むドキュメントを読み、各ステップを role-mode の `mode:` / `role:` ヘッダを付けた隔離 subagent ターンとして実行する。LLM 向けの仕様は本スキルの `SKILL.md`（`skills/mode-orchestrator/` 配下）にある。workflow spec の書き方は同じフォルダの `WORKFLOW_SPEC_AUTHORING.md` を参照。（英語版: `USER_GUIDE.md`）

## 何をするか

- todolist + 関連 context を含む1つのドキュメントを受け取る。
- 各ステップについて **mode**（任意で **role**）を選び、その mode の NEVER/DO ルールを載せたプロンプトを組み立て、**1つの隔離 general-purpose subagent ターン**として実行する。1ターンにつき 1 mode（role は最大1つ）、混在なし。
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

## 起動 — フラグ

| フラグ | 効果 |
|---|---|
| _(なし)_ | turn plan を提示し、実行前に承認を待つ。 |
| `--auto` | 承認ゲートを飛ばし、全ターンを承認なしで実行する。 |
| `--roles` / `--roles=always` | 各ターンに適合する role を推論して付与する。デフォルト: role は推論しないが、todolist に明示された role は honor する。 |
| `--workflow=<name>` | workflow spec（`workflows/<name>.md`）を defaults として読み込む。todolist 内で spec 名を宣言しても同様に honor される。デフォルト: spec なし — todolist の指定どおりに実行。 |

フラグは意図的に `--` 形式を使う。`mode:` / `role:` のコロン接頭辞は使わない（role-mode フックに捕捉されるため）。

## Mode

**Autonomous**（subagent ターンとして実行）: `survey`, `plan`, `execute`, `debug`, `review`, `review-dev`。エイリアス: `verify` → `debug`, `implement` → `execute`。

**Interactive**（実行しない — ネイティブ実行の提案として提示）: `ask`, `discuss`, `brainstorm`, `organize`。これらは autonomous subagent には提供できない、人間とのライブなやり取りを必要とする。

mode は hybrid で決定: ステップが mode を名指ししていれば honor、なければステップ内容から適合 mode を推論する。

## ターンごとの model

各ターンは特定の model で実行できる。model は優先順位で解決され、mode 単独からの推測はしない:

1. **ステップ明示** — todolist のステップで名指しされた model（または有効な workflow spec がそのステップに pin した model）。衝突時は todolist が勝つ。
2. **spec 表** — 有効な workflow spec の mode→model デフォルト。
3. **継承** — override なし。セッションの model を使う。

turn plan は各ターンの model と、どの段が決定したかを表示する。

## Failure リカバリループ

`execute` ターンが planned check（例: テスト）を走らせて失敗し、その失敗がリポジトリ内で修正可能に見える場合、status `failed` を返し **Failure report**（Error / Reproduction / Error output / Target file(s) / Context）を書く。オーケストレータは次に:

1. `debug` ターンを挿入 — 根本原因を診断し最小 diff を提案する（diff の適用は自身では行わない）、
2. re-execute ターンを挿入 — その diff を適用し check を再実行する、
3. 通れば本流に復帰、通らなければもう1サイクル回す。

ターンごとの**サイクル上限**はデフォルト 2（workflow spec で上書き可）。上限に達してもなお失敗している場合は `blocked` に格上げして run を停止する。`debug` ターンが `needs-human`（例: 修正がタスクの許可範囲外）を返した場合も run を停止する。

### リカバリループに入らないもの

診断する価値があるのは `failed`（作業は走った・check が通らなかった・ここで直せそう）だけ。次の3つは意図的にループを迂回する:

- **`blocked`** — ツール呼び出しが**権限システムに拒否**されたターンを含む。拒否はリポジトリ内のバグではないので、再実行しても毎サイクル同じ壁に当たる。run を止めて利用者に尋ねる。
- **`needs-human`** — 利用者にしか下せない判断が必要なターン。
- **`aborted`** — タスクについて**何も報告していない**ターン。返答に所定の status 行が無い（中断・kill・契約逸脱）場合と、そもそも返答が来ず**ターン watchdog**（後述）が打ち切った場合の両方を含む。診断すべき失敗が存在しないので、orchestrator はそのターンを**1回だけ**再実行し、それでも読めなければ `needs-human` で停止する。この再実行はサイクル上限を消費しない。

各ターンは返答の**最終行**を `status: <...>; file: <path>` に固定し、orchestrator はその行だけから結果を読む。だから status 行を欠いたターンは推測せず `aborted` として扱う — 沈黙した／壊れたターンが成功として通り抜けるのではなく、はっきり失敗する。

**`failed` を提示されるのは `execute` ターンだけ。** この status は「planned check が通らなかった」と定義されており、その check を走らせるのは `execute` ターンだから。他のモードには3値契約（`ok` / `blocked` / `needs-human`）を渡す。それでも他のターンが `failed` を返した場合は契約違反として扱い、リカバリループ（診断すべき Failure report が存在しない）には入れず `needs-human` で run を停止する。

## ターン watchdog

status 行が分類できるのは**返答してきたターンだけ**。subagent は返答自体をやめることもある — kill・中断・単なるハング — この場合は通知すら来ない。orchestrator はそれを自力で気づけない: 何かに起こされたときにしか動かないので、来ない返答を待つことは終わらない待機になる。

そこで各ターンは、背景で走る **watchdog**（`scripts/watchdog.sh`）と一緒に起動する。watchdog は次のうち最も早く起きたものを報告して終了し、**その終了自体が orchestrator を起こす**:

- ターンの**成果物ファイル**が非空で出現 — `DONE`;
- 成果物が無いまま**ウォールクロック期限**を超過 — `TIMEOUT`;
- subagent の transcript が停滞閾値のあいだ**伸びなくなる** — `STALL`。

watchdog は報告するだけで、自分では何も停止しない。`TIMEOUT` / `STALL` を受けてターンを停止し `aborted` に分類して1回だけ再実行するのは orchestrator の側 — 読めない返答とまったく同じ扱い。診断はしない。何も報告しなかったターンには診断する材料が無いため。

既定値はスクリプト冒頭にある。変更はそこで行う:

| 設定 | 既定値 |
|---|---|
| 停滞閾値 | 600s |
| 期限 `survey` | 600s |
| 期限 `plan` / `execute` | 1500s |
| 期限 その他のモード | 900s |
| ポーリング間隔 | 15s |

意図的に余裕を持たせてある。長いツール呼び出しは外から見るとハングと区別がつかない（どちらも transcript は静止する）ので、停滞閾値はターンが行いうる最長のツール呼び出しより十分大きく取る。これは「本物のハングの検出が遅れる」代わりに「作業中のターンを切らない」側に倒す判断。まだ進行中だったターンが繰り返し abort されるなら予算が厳しすぎる。どの検査が発火したかは run index に記録されるので、それを見て引き上げられる。

watchdog が縛るのは時間であって正しさではない。「ターンが長すぎた」ことは言えるが、「ターンが間違ったことをした」ことは言えない。

## Workflow spec

workflow spec は1つのタスク種別に対する **defaults とガイダンス**（推奨ステップ列・mode→model 表・failure policy の上限）を、エンジンを変えずに供給する。spec は弱結合: todolist が常に正であり、todolist と spec の不整合は**警告**として表れるだけでリジェクトにはならない。

本スキルは開発／実装作業向けの spec **`dev`**（`workflows/dev.md`）を同梱する（調査 → 設計 → レビュー → 実装 → テスト → レビュー → docs 同期）。`--workflow=dev` または todolist 内での名指しで有効化する。別のタスク種別の spec を追加するには `WORKFLOW_SPEC_AUTHORING.md` を参照。

## run ディレクトリと成果物

起動ごとに workspace に run ディレクトリを1つ作る（例: `mode-orchestrator-runs/<run-slug>/`）:

- `NN-<mode>.md` — ターンごとの deliverable を順に。リカバリターンは接尾辞形式 `NNa-debug.md` / `NNb-execute.md` / `NNc` / `NNd` を使う。
- `index.md` — turn plan（各ターンの model と段を含む）、spec 警告、Failure policy、各ターンの status。`aborted` を受けて再実行したターンもここに記録する — aborted ターンは deliverable を書かないため、それが起きた事実を残す唯一の場所になる。検査用インデックスであり、再開可能なスケジューラではない。

これらはランタイム成果物 — コミットしない。

## しないこと

- `execute` ターン周りの rollback / checkpoint / worktree は無い — 作業ツリーの安全はあなたの git 衛生の責務。autonomous な run の前に WIP を commit / stash すること。
- run 途中の対話的 handoff や再開スケジューラは無い。
- 並列ターンは無い — ターンは順番に実行される。
- ゼロ分解は無い — todolist は入力に存在している必要がある。

change-report が「どのファイルを編集したか」と主張する内容ではなく、run の**最終状態**（planned check の再実行）を信頼すること。change-report は自己申告である。

## 関連ドキュメント

- LLM 向け仕様: `SKILL.md`（`skills/mode-orchestrator/` 配下）。
- workflow spec の書き方: 同じフォルダの `WORKFLOW_SPEC_AUTHORING.md`。
- 同梱の `dev` spec: `skills/mode-orchestrator/workflows/dev.md`。
