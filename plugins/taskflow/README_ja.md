# taskflow

Claude Code プラグイン。同時並行するタスクの進捗とコンテキストを管理する。セッションをプロジェクトに紐付け、`progress.md` / `tasks/` / `project-notes/` による状態遷移とコンテキスト注入を提供する。

[English README](README.md)

> **taskflow は初めて？** まず [はじめよう](GET_STARTED_ja.md) で最初の成功までをたどる。[ユーザーガイド](USER_GUIDE_ja.md) は詳しい利用リファレンス、この README は機能リファレンス、[`docs/architecture.md`](docs/architecture.md) は内部設計。

## インストール

### マーケットプレイス経由（推奨）

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install taskflow@ai-agent-toolkit
```

### ローカル（開発・テスト用）

```bash
claude --plugin-dir ./plugins/taskflow
```

## セットアップ

手動セットアップは不要。workspace で初めて `pj:<project>` を使用したタイミングで、`UserPromptSubmit` フックが `_projects/`、`_projects/_state/`、`_projects/index.md`（テンプレート）を自動生成する。

> **Claude Code 専用。** taskflow の毎ターン project routing は `UserPromptSubmit` の `additionalContext` 注入に依存している。Cursor の third-party 互換で auto-map される `beforeSubmitPrompt` は LLM コンテキスト注入を持たない（block 専用）ため、taskflow は Cursor 上では動作しない。背景は `_projects/harness-taskflow/project-notes/procedures/claude-plugin-to-cursor-compat.md`（開発リポジトリの設計メモ。プラグインには同梱されない）を参照。

### 古い workspace の移行

upgrade で壊れるものは無い。古い release で書かれた workspace を現行の前提に揃える one-time script が 2 本ある。どちらも既定は dry run で、既に適合しているものは skip するので、再実行してよい。

#### task log の session tag — 0.2.8 以前で書かれたもの

`@log` の各 entry は、それを書いた session を `[s:<hex>]` で持つ。0.2.8 までは session id の先頭 8 hex で、これは衝突する。現在は dehyphenate した id の末尾 12 hex。古い entry は短い tag のまま残り、どの session を指すのか引き直せない。

```bash
# dry run — 何を書き換えるか報告するだけで、書き込まない
uv run python <taskflow>/scripts/migrate_sid_tags.py --root <workspace>
# 報告を読んでから適用
uv run python <taskflow>/scripts/migrate_sid_tags.py --root <workspace> --write
```

`--root` の既定は現在の directory で、`_projects/` を持つ必要がある。tag を書き換えるのは、その 8 桁が `_projects/_state/` 配下のちょうど 1 つの session state file に解決したときだけ — まず prefix で、次に stem 自身の末尾 8 桁で照合する。2 つ以上に当たる tag と、1 つも当たらない tag（state file が既に sweep された場合）は報告して放置するので、古い workspace では部分的な移行になることを見込んで報告を読むこと。

`@log` の append-only 規則を免除するのはここだけである。task ファイルは gitignored で復元手段が無いため、変更する file はすべて事前に `<name>.md.bak-sidmigrate` として隣にコピーする。触るのは対になった `<!-- @log:begin -->` / `<!-- @log:end -->` の間だけで、legacy tag を持つのに marker が対になっていない file は推測せず stderr に名前を出して skip する。

#### project-notes の frontmatter — 0.3.0 以前で書かれたもの

既存の `project-notes/` は本 release の恩恵を新規 note ほど受けられない。毎ターンの push（[project-notes](#project-notes) 参照）は、以前は存在しなかった frontmatter 2 field を読む: `summary:`（本文を開かずに判断するための text）と `anchors:`（turn との場所一致を成立させる repo 相対 path）。どちらも無い note は topic 層からしか届かず、100 code point 上限より前に書かれた `index.md` の Description は注入行で切り詰められる。

この 2 つを埋めるのが `scripts/backfill_notes.py`。workspace ごとに 1 回実行する:

```bash
# dry run — manifest を書くだけで、何も編集しない
uv run --script <taskflow>/scripts/backfill_notes.py <workspace>
# manifest を読んでから適用
uv run --script <taskflow>/scripts/backfill_notes.py <workspace> --apply
```

`<workspace>` は `_projects/` を持つ directory で、その配下の全 project が対象になる。Stage 1 は Claude に 1 call あたり 8 note ずつ `summary:` / `index.md` の Description / `anchors:` list を問い、`<workspace>/tmp/notes-backfill/<timestamp>/<project>-manifest.json` に書く。Stage 2（`--apply`）はその manifest を契約の上限に照らして検証し、note と `index.md` を編集する。契約を満たす `summary:` と `anchors:` key を持ち Description が 100 code point 以内の note は skip し、`index.md` 自身と `_archive/` 配下も対象外なので、途中まで移行した workspace に再実行してよい。

必要なのは `uv`、起動できる `claude` binary（PATH 上のものが shell shim の場合 — Windows はそうなる — は `CLAUDE_BIN` を設定する）、および network。note ごとの判断は model が行うため。実行後は `scripts/check_progress.py <workspace>/_projects/<project>` で検証する（summary の長さ、summary の充足率、dangling anchor を報告する）。

この上に topic 層を有効化するかは別の任意手順 — [project-notes](#project-notes) を参照。

## 設定

以下の変数はすべてプロセス環境から読む。シェルで export するか、Claude Code で恒久設定するなら `settings.json` の `env` ブロックに置く: `~/.claude/settings.json` は全 project、workspace の `.claude/settings.json` または `.claude/settings.local.json` はその workspace だけに効く。

`settings.json` 経由に頼る前に知っておくべき性質が 2 つある（2026-09-14 に Claude Code 上で `TASKFLOW_QMD_TIMEOUT` を用いて実測。loader は変数ごとではないが、実際に動かしたのはこの 1 変数のみ）:

- **新しい値は session を再起動しなくても効く。** 次のターンでもう反映されており、hook も見る。qmd 子プロセスは hook の環境を継承する — `run_qmd` が `env=` 引数なしで spawn しているためで、これは subprocess の既定挙動を source から読んだものであり、個別に実測した主張ではない。
- **キーを削除しても稼働中の session からは消えない。** 値は session の環境に残り、別の値で上書きするか session を再起動するまで生き続ける。実測では、削除したキーが元ファイルの byte 同一な revert をまたいで 8 時間生存した。誤った値を直すときは、消すのではなく正しい値を書くこと。

### `TASKFLOW_PROJECT_ROOTS`

セミコロン区切りの `_projects/` ルートディレクトリ一覧。skill とスクリプトがこの変数を参照し、複数リポジトリにまたがるプロジェクトデータを検索する。

```bash
export TASKFLOW_PROJECT_ROOTS="/path/to/repo-a/_projects;/path/to/repo-b/_projects"
```

未設定の場合、現在の workspace の `_projects/` にフォールバックする。

Claude Code で恒久設定するには `settings.json` に追加:

```json
{
  "env": {
    "TASKFLOW_PROJECT_ROOTS": "/path/to/repo-a/_projects;/path/to/repo-b/_projects"
  }
}
```

### `TASKFLOW_GUIDELINES_REMINDER`

`session_init.py` が毎ターン注入する guidelines reminder の variant を選択する: `full`（デフォルト、~750 tok — 完全版キーワードリマインダー）または `manifest`（~460 tok — PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE の各ルールを recall label に圧縮したもの。毎ターン無条件発火が要件の ROUTER と RESPONSE LEADING LINES の2行はどちらの variant でも全文のまま）。未知の値・未設定は `full` にフォールバックする。

```json
{
  "env": {
    "TASKFLOW_GUIDELINES_REMINDER": "manifest"
  }
}
```

`manifest` は毎ターンコストを下げる代わりに、条件付きルール（PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE）のインライン可視性が下がるトレードオフを伴う — セッション開始時（および compact 後）に注入された全文ガイドラインへの依存度が上がる。

### `TASKFLOW_CONTEXT_DONE_ROWS_MAX`

エージェントの**コンテキスト**に入る Completed 行数の上限。`progress.md` 自体は常に `tasks/2_done/` の全件を保持し、`scripts/view_progress.py` が truncate した写しを stdout に出力する。注入される In Progress digest より広い範囲が要るとき、メインエージェントが読むべきなのはその写し。project router は判断のために script を走らせるが、その出力を返すことはない。既定値: `10`。`0` で無制限。行が落ちた場合のみ `[context view]` 脚注が省略件数を示す。

```json
{
  "env": {
    "TASKFLOW_CONTEXT_DONE_ROWS_MAX": "20"
  }
}
```

`scripts/view_progress.py --limit N`（または `--all`）で単発呼び出し時に環境変数を上書きできる。

> 0.2.6 で撤去: `progress.md` をディスク上で truncate していた `TASKFLOW_DONE_ROWS_MAX` と `rebuild_progress.py --done-rows-max`。旧変数を設定しても効果はない — ファイルはもうキャップされない。

### `TASKFLOW_LOCK_TIMEOUT`

writer が advisory write lock（`hooks/log_lock.py`）の取得を諦めるまでの待ち時間（秒）。既定値: `3.0`。取得は **bounded** — 期限切れ時はブロックせず、stderr に警告を出したうえで *ロックなしのまま* 書込を続行する（スタックしたロックがセッションを停止させることはない）。値を下げるほど、競合時に早くロックなしへ degrade する。

```json
{
  "env": {
    "TASKFLOW_LOCK_TIMEOUT": "3.0"
  }
}
```

### `TASKFLOW_LOCK_STALE`

他の writer がロックのサイドカーを破棄して所有権を奪えるようになるまでの、サイドカーの経過時間（秒）。既定値: `10.0`。このロックは「存在＝ロック」方式でカーネルが保証する所有者を持たないため、強制終了された writer が残したサイドカーはこの閾値を過ぎて初めて回収される — この bounded なクラッシュ後の待ちは、掃除すべき孤児ロック状態を構造的に持たないことの対価である。通常の保持時間はサブミリ秒。想定される最長保持時間より短く設定すると、*生きている* holder のサイドカーが破棄されうる。

```json
{
  "env": {
    "TASKFLOW_LOCK_STALE": "10"
  }
}
```

いずれも呼び出しごとに読み直される。Pi の taskflow 拡張も同じ 2 つの変数名を読むため、両者で同一の値を設定すること（異なる値では、2 つのハーネスが別々のスケジュールで待機・期限切れする）。

### `TASKFLOW_QMD_BIN`

qmd の `bin/qmd` スクリプトのパス。未設定（既定）なら `PATH` 上の `qmd` を解決し、その隣の `node_modules/@tobilu/qmd/bin/qmd` を `node` で起動する — `.cmd` shim は実行しない。使う経路は 3 つあり、gate はそれぞれ別:

- 毎ターンの project-notes push（[project-notes](#project-notes) 参照）。project に `project-notes/.qmd/` directory があるときに使う。解決できない場合、push は lexical rank へ degrade し、degrade が続く間は毎ターン告知する。
- Stop hook の qmd index 追随（[hook](#hook) 参照）。gate は同じ directory の有無。
- `/progress check` の `vector coverage:` footer。gate はこれだけ `project-notes/.qmd/index.yaml` または `index.yml` という **file** の有無 — directory だけでは qmd が上位へ walk して、祖先または global の index をこの project の数値として報告してしまう。binary を解決できないときは footer 行を出さないだけで、他には何も起きない。

後者 2 つは `TASKFLOW_QMD_TIMEOUT` を読まず、いずれの上限も上書きできない。追随の `qmd update` は 5 秒、その `qmd status` probe は 3 秒（加えて 1 回の Stop 内の全 probe が 4 秒の wall-clock 予算を共有）、footer の `qmd status` は 10 秒で固定である。

```json
{
  "env": {
    "TASKFLOW_QMD_BIN": "/path/to/node_modules/@tobilu/qmd/bin/qmd"
  }
}
```

### `TASKFLOW_NOTES_PUSH`

毎ターンの project-notes push がどの層を使うか。`auto`（既定）: 場所（`anchors:` と、そのターンが名指し・Read・書込した path の照合）＋ `project-notes/.qmd/` があるときの qmd 構造化 query ＋ プロセス内 lexical rank を交互に取った和集合で合成。`lexical`: 場所＋ lexical のみ（qmd は起動しない）。`off`: 場所のみ。

同じ軸が Stop hook の qmd index 追随（[hook](#hook) 参照）も gate する。走るのは `auto` のときだけで、`lexical` と `off` はいずれも「この tree では qmd を起動しない」を意味する（維持のための書込も例外ではない）。

```json
{
  "env": {
    "TASKFLOW_NOTES_PUSH": "lexical"
  }
}
```

### `TASKFLOW_QMD_TIMEOUT`

毎ターンの project-notes push が 1 回の qmd query に許す秒数。既定 `6`。query は `project-notes/.qmd/` index を持つ project の毎ターン、prompt 注入経路で同期に走る — 参照 machine（GPU embedding、79〜210 note）で 2.7〜2.9 秒の実測で、これは明示的に受容したコスト。予算は cold / busy な machine を有界にするためのもので、通常経路の調整ではない。超過したターンは lexical rank へ fallback し、起きたターンのたびに告知する。数値でない・正でない値は既定に戻る。Pi の taskflow 拡張も同じ名前を読むため両側で同一に設定すること。ただし経路は同じではない — Pi の設定には `env` ブロックが無く、拡張はプロセス環境からのみ値を取る。両側に効かせるには OS レベルのユーザ環境変数を使うことになり、既に動いているプロセスは起動時の値を持ち続ける。

```json
{
  "env": {
    "TASKFLOW_QMD_TIMEOUT": "6"
  }
}
```

cold のケースは机上の話ではない。参照 machine では、同一 run の他の call が 2.7〜2.8 秒だったのに対し 1 件だけ 8.83 秒だった（観測 1 件）。それは起動直後のプロセスの初回 call で、何がその call を遅くしたのかは特定していない。harness 起動の 16 秒後に始まった session は 1 ターン目が degrade し、次のターンで回復した。ターン 1 で vector 層を失うことが問題なら値を上げること。告知は超過した予算を名前に含む（`qmd timeout after 6s`）ので、timeout は他の degrade 原因と区別できる。

## 使い方

### プロジェクト指定

`pj:プロジェクト名` はプロンプトの冒頭（最初のほう）に置く。行頭または空白直後ならどこでも認識されるため、他の先頭行（`mode:` など）の後でもよく、物理的な先頭である必要はない。**`pj:` はメッセージの先頭 500 字以内に記述した場合のみ認識される。** 省略時は直前に設定済みのプロジェクトを維持する（文脈からの推定は行わない）。

| 操作 | プロンプト例 |
|---|---|
| プロジェクト指定 | `pj:my-project ビルドエラーを直して` |
| プロジェクト指定 + コマンド | `pj:my-project /plan スキーマを設計せよ` |
| プロジェクト探索 | `pj:?` または `pj:? デプロイパイプライン` |
| プロジェクト該当なし | `pj:none READMEを書いて` |
| 新規プロジェクト作成 | `新しいプロジェクト xxx を作って` |
| taskflow を完全バイパス（このターン） | `norouter READMEを書いて` |

### /progress — タスク進捗コマンド

`/progress` はタスク状態の参照・変更の統一窓口。自然言語入力を `progress-router` サブエージェントが (action, targets) に解釈し、`AskUserQuestion` で確認（`-y` 指定時はスキップ）して実行する。

| sub-action | 効果 | 例 |
|---|---|---|
| `check` | drift / stale / 承認待ち検出に加え、notes のデータ契約（flat layout、`summary:` ≤500 字、`anchors:` を query-lsp snapshot `<repo-root>/.lsp-cache/snapshot.json` または `--lsp-snapshot` で解決）を検査。末尾に always-on footer を出す（いずれも finding ではなく、exit code にも影響しない）: `notes without summary: n of N`、`near-duplicate note pairs: n`、query-lsp snapshot が無かったときの `anchor symbols unchecked: n`、そして `vector coverage: n of N docs` — `qmd status` の `Total - Pending` と `Total`（いずれも doc 単位）。`project-notes/.qmd/index.yaml`（または `index.yml`）があり、かつ qmd が応答したときだけ出る。read-only。 | `/progress check` |
| `audit` | 各タスクを `## Next Steps` 状態で 4 区分（pending / completion candidate / untracked / clean）に分類。read-only。 | `/progress audit` |
| `rebuild`（`sync` は別名） | progress.md のテーブル領域を task ファイル群から再生成。 | `/progress rebuild` |
| `start` | `0_todo/ → 1_in_progress/` への移動（完了タスクの再開 `2_done/ → 1_in_progress/` も担当）。 | `/progress start 2026-05-14_xxx`<br>`/progress 着手 migration` |
| `approve` | `1_in_progress/ → 2_done/` への移動。人間承認の遷移。（`0_todo/` からは 1 段飛ばしの jump として ⚠ 付きで確認。） | `/progress approve 2026-05-14_xxx`<br>`/progress 完了 migration`<br>`/progress 全部完了 -y` |
| `unstart` | タスクを未着手（TODO）へ移動（`1_in_progress → 0_todo`。`2_done` からは 1 段飛ばしの jump として ⚠ 付きで確認）。 | `/progress unstart <prefix>`<br>`/progress migration を未着手に` |

**state 目的語の同義語** — ユーザは到達させたい state を名指す（英語トークンは語境界一致 — 部分語ヒットなし・パス内は不算入、日本語トークンは substring 一致で重なりは最長優先、大文字小文字無視）:

- approve（`2_done`）: `完了`, `終了`, `done`, `finish`, `approve`
- start（`1_in_progress`）: `着手`, `開始`, `再開`, `進行中`, `start`, `begin`, `resume`
- unstart（`0_todo`）: `未着手`, `着手前`, `開始前`, `todo`, `unstart`
- `check` / `audit` / `sync` / `rebuild`: literal キーワードのみ
- undo/revert 語彙（`戻す` / `undo` / `revert` / `取り消し`）は意図的に不採用 — これらは LLM 行動の undo（例: グローバル revert skill）の領域であり、該当入力は `unknown` となり何も移動しない。

**target 解決**（優先度の高い match から採用）:

1. filename stem の先頭一致（大文字小文字無視）
2. filename stem の部分一致
3. H1 のセマンティック一致
4. plurality 表現（`全部` / `all` / `両方` / `両` 等）→ 全候補が match

**フラグ**:

- `-y` / `--yes` — 確認プロンプトをスキップして即実行

destructive アクション（`approve` / `start` / `unstart`）では、メインエージェントが解決済 plan を表示し `AskUserQuestion` で確認を取ってから初めて mutation を実行する。`-y` で対象が確定済の場合はスキップ可。0 match / 低 confidence の複数 match の場合、router は候補を列挙して停止する（推測進行しない）。

### /pj-rules — プロジェクト固有ルール

`/pj-rules` は、プロジェクトが任意で持つ `_projects/<project>/rules.md` — taskflow プロジェクト（`pj:`）単位でスコープされた規範（ファイルパス単位ではない）— を閲覧・編集する。router subagent は使わない（action 集合が小さく、write の本文はどのみち主エージェントが構成するため）。intent は小さな同義語表で inline 分類する。

| Action | 動作 | 例 |
|---|---|---|
| `show`（別名 `list`） | ルール本文・`## ` 見出し数・行数 vs cap を表示。read-only、確認不要。 | `/pj-rules show` |
| `write` | ルールの追加/編集、または `inject_every_turn`/`max_lines` frontmatter の変更。常に diff として提示し `AskUserQuestion` で承認後に適用。 | `/pj-rules add a rule: never edit dist/ directly` |

**`write` に `-y` スキップは無い。** `/progress` と異なり、この skill には確認バイパスが存在しない — `rules.md` はプロジェクトの以降の全ターンに注入されるため、blast radius が当該ターンを超えて及ぶ。入力中の `-y`/`--yes` は無条件に無視される。

承認済み write の後、`scripts/pj_rules.py show` を前後で実行して編集が `## ` 見出しを生成したか検証し（モデルの自己申告を信用しない）、続いて `scripts/pj_rules.py reset-indexed` で session state の `project_rules_indexed` フィールドのみをリセットする（merge-preserving — 他の state フィールドは無傷）。これにより次ターンで更新後の全文が再び表示される。

### /kanban — Kanban プロジェクトボード

`/kanban` はすべての taskflow プロジェクトとそのタスクを kanban ボードで表示する自己完結型 HTML を生成する。タスクはステータス（未着手・作業中・完了）とプロジェクト別に整理され、優先度バッジ、セッション履歴、セッションログ or `/progress` サブコマンドへのワンクリック遷移を提供。

kanban ボードの特性:
- `_projects/index.md` からすべてのプロジェクトを読み込み、タスク一覧を列挙
- 各タスクの `@log` ブロックからセッション履歴を抽出し、short session ID を full UUID に解決（クリッカブルリンク化）
- 2 つのビューを備える（トグル切り替え）：**By Status**（ステータス別列）と **By Project**（プロジェクト別列）
- legend ボタンでプロジェクト・ステータスのリアルタイムフィルタリング、および手動のライト / ダークテーマトグルを実装
- `/progress check`, `/progress audit`, `/progress rebuild` への quick-access dropdown を装備
- 各カードの **▶ CC** ボタンでタスクを Claude Code に起動（`pj:<project> @<タスクファイル>` をプリフィル）
- 各タスクの Markdown をブラウザ内ビューア（**📄**、serve モード）で表示：サニタイズ描画・クリッカブルなファイル参照（モーダル内遷移＋戻るボタン）・インライン画像
- 未参照セッションを可視化：プロジェクトに属すがタスク未紐付けの CC セッションを **No Task** 列／プロジェクト列内セクションに、どのプロジェクトにも属さない CC セッションを最右の **No Project** 列に表示（新しい順・上限あり）

起動方法：

| 方法 | コマンド | 結果 |
|---|---|---|
| skill 経由 | `/kanban` | サーバーをバックグラウンドでワークスペース由来の `http://localhost:<port>/` に起動（冪等 — このワークスペース向けに既に稼働中なら `already serving` を報告）し、URL と `--stop` コマンドを表示する（ブロックしない） |
| script（静的） | `uv run scripts/generate_kanban.py` | HTML を `/tmp/taskflow-kanban.html` に出力 |
| script（サーブ） | `uv run scripts/generate_kanban.py --serve --open` | サーバー起動＋ブラウザ自動起動 |

各ワークスペースのサーバーは、その `_projects` roots から導出したポート（base `17329`・span 64、プロセスを跨いでも `hashlib` により決定論的 — 同一ワークスペースからの後続の `--stop` が同じサーバーを発見できる）で待受する。複数の VSCode ワークスペースで同時に `/kanban` を実行してもポートが衝突しなくなった：各ワークスペースが個別のポートを持ち、`/health` にワークスペース識別キーを含めることで、ポートのハッシュ衝突が起きても別ワークスペースのサーバーを「既に稼働中」と誤認しない。

> **Claude Code を開くリンク**（**▶ CC** ボタンおよびセッション・プロンプト起動）は、Claude Code の VS Code / VSCodium 拡張（`anthropic.claude-code`）を**前提**とする。起動経路はこの拡張のみで、端末（CUI）での Claude Code 起動は**非対応**——フォールバックとしても行わない。serve モードでは起動用 CLI（`codium` → `code` の順）を解決し、`--list-extensions` でプローブする。確定的な結果——拡張あり／拡張なし／CLI が `PATH` に無い——はサーバーのプロセス寿命の間キャッシュされるため、プローブは実質 1 回で済む。プローブの失敗（例外・15 秒タイムアウト・非ゼロ終了）は**キャッシュしない**：stderr に 1 行ログを出し、次のクリックで再試行する。プローブの失敗は拡張が無いことの証拠にはならないため、リンクはそのまま楽観的に起動し、ページには「未インストール」と断定せず拡張の実在を**検証できなかった**旨を表示する。リンクを起動しなかった場合（CLI が無い／拡張の不在が確定した場合）、および検証できないまま起動した場合、ページは独立したブロックにコピー可能なフォールバックを載せる：セッションリンクなら `claude --resume <uuid>`（`claude` CLI が `PATH` に無ければ UUID のみ）、▶ CC・`/progress` のプロンプトリンクなら貼り付け用のプロンプト本文（CLI には未送信プリフィルに相当する入口が無いため）。

> **`CLAUDE_CONFIG_DIR`** — Claude Code の config ディレクトリを移設する場合は、**マシン全体で 1 つの値**を、**絶対パス**で、Claude Code セッション・`kanban serve` プロセス・VS Code 拡張ホストのすべてで同一に設定すること。ワークスペース別に異なる値を持つ運用は**非サポート**：拡張ホストの環境変数は taskflow の制御外であり、taskflow のデータモデルはセッション UUID しか記録せず、どの config ディレクトリ由来かの情報を持たないため、値が分岐した構成は無音で失敗する（▶ CC リンクやセッションリンクが解決不能になる）。kanban のリーダーは `$CLAUDE_CONFIG_DIR` と `~/.claude` の両方を走査し、UUID が衝突した場合は env 側を優先する。session-sync フックは env の値のみを使う。なお Claude Code はこの値を**リテラル**として解釈する — `~` は展開されず、相対値はカレントディレクトリ基準で解決されるため、`~/foo` を指定すると CWD 配下に `~` という名前のディレクトリが作られる。

script のオプション:

- `--out PATH` — HTML 出力先を指定（デフォルト：システム一時ディレクトリ / `taskflow-kanban.html`）
- `--serve` — ワークスペース由来のポートで HTTP サーバーを起動。endpoints：`/open?session=<UUID>`・`/open?prompt=<...>`（セッション・プロンプト起動）、`/md?path=<file>`（サニタイズ済み Markdown 描画）、`/file?path=<file>`（プロジェクト配下の画像・添付配信）、`/health`
- `--stop` — このワークスペースの稼働中 `--serve` を停止（`/health` の pid 経由）
- `--stop --all` — 全ワークスペースの kanban サーバーをすべて停止
- `--port PORT` — 導出ポートの代わりに明示ポートを使用（`--serve`／`--stop` 両対応）
- `--open` — 生成後、デフォルトブラウザで自動起動
- `--scheme vscode|vscodium` — URI scheme を上書き（デフォルト：自動検出）

### progress.md

`progress.md` はタスクの index。手書き自由文セクション（Architecture / Key Decisions / Open Issues / Reference Materials）と、auto-generated テーブル領域（`<!-- @table:begin -->` ... `<!-- @table:end -->`、TODO / In Progress / Completed の各表）で構成される。テーブル再生は `/progress rebuild`、マーカー内側は手編集禁止。Completed セクションは `tasks/2_done/` の全件を列挙し、ファイルが truncate されることはない。上限が掛かるのはエージェントのコンテキストに入る分だけで、担当は `scripts/view_progress.py`（[設定](#taskflow_context_done_rows_max) 参照）。

### tasks

1 タスク 1 ファイル、`tasks/<status>/<date>_<topic>.md`。status はフォルダで表現する。

```
tasks/
  0_todo/             未着手
  1_in_progress/      作業中
  2_done/             完了（人間承認済）
```

task ファイル構造:

```markdown
---
priority: HIGH
created: 2026-05-13
updated: 2026-05-14
---

# タスクタイトル（progress.md の row summary になる）

本文（mutable 領域 — 自由に置換可能）。

## Next Steps
- 残作業項目 1
- 残作業項目 2

<!-- @log:begin -->
- 2026-05-13 [s:abc123456789]: 着手
- 2026-05-14 [s:def678901234]: phase A 完了 | next: テスト追加
<!-- @log:end -->
```

- 本文領域は自由編集、`<!-- @log -->` ブロックは **append-only**。
- `## Next Steps` が非空 = pending、`1_in_progress/` で空 = 完了候補。ガイドラインがエージェントにタスクを前進させたターンの終わりに `## Next Steps` を維持するよう指示し、`/progress audit` がコードで検査する（[仕組み](#仕組み) 参照）。
- log 行には `[s:<session-id 末尾12桁>]` タグが付き、audit の参照用 index になる。
- task には、関連する `project-notes/` パスを列挙する自動管理ブロック `<!-- @notes:begin/end -->`（`@log:end` の直後に配置）が付くことがある。note↔task link 機構が書き込む（[仕組み](#仕組み) 参照）ため手編集禁止。

status 遷移は `/progress start` / `/progress approve` / `/progress unstart` で行う（上記参照）。

### project-notes

プロジェクト固有の永続知識、folder でカテゴリ分類:

| カテゴリ | 用途 |
|---|---|
| `specs/` | 仕様・設計・決定・ADR |
| `investigations/` | 調査・分析・post-mortem |
| `checks/` | 確認項目・チェックリスト（判定なし） |
| `procedures/` | 人間向け手順書 |
| `backlog/` | 候補・アイデア・issue |
| `_archive/` | 役目終了 |

| 操作 | プロンプト例 |
|---|---|
| 保存 | `この調査結果をnotesに保存して` |
| 一覧確認 | `notesに何がある？` |
| codebase 記録 | `このリポの構造をnotesにまとめて` |

`project-notes/index.md` は 4 列テーブル（`File | Description | Tags | Updated`）で notes を管理する。notes 作成・更新時に LLM が（PreToolUse フックの促しを受けて）同期する。note の frontmatter には `summary:`（≤500 字、本文を開く前に読む判断材料）と `anchors:`（その note が語る code を repo 相対 `path` または `path::symbol` の list で示す）を置ける。`/progress check` が両方を検査し、layout は flat（`<category>/<name>.md`、sub-directory は `_archive/` 配下のみ）。

project-router は project-notes を **一切返さない**。note の選別は決定論の毎ターン push に置き換わって router から外れており、agent は `project-notes/` 配下を何も読まない（`index.md` も本文も directory 一覧も）し、notes block も返さない。したがって note 内容を要約・翻訳・confabulate して routing 結果に混入させることがない。note がメインエージェントに届く経路は、次に述べる `project-notes (index rows):` block と `query-lsp` の symbol 経路の 2 つで、本文はメインエージェントが直接読む。

さらに `session_init.py` が毎ターン `project-notes (index rows):` block を注入する — `index.md` の行を逐語で最大 8 行、**場所**（そのターンが名指し・Read・書込した file を `anchors:` に持つ note が先頭、同じ directory に anchor する note がその次で最大 4 行）と **topic**（`project-notes/.qmd/` があるときの note 本文への qmd 構造化 query と、Description ＋ Tags のプロセス内 lexical rank を交互に取った和集合で合成）で選び、末尾に `project-notes matched: M of N rows (K shown, S strong)` を付ける。`_archive/` 行は除外し、note 本文は注入しない。`M` は候補数 — 場所一致の両層と、topic 層が返した行（qmd 行も lexical 行も）の和集合 — なので、実際に載る `K` 行を上回りうるが、それは良い候補が切られたことを意味しない。`S` はより狭い数で、file 粒度の場所一致（第 1 層のみ。同じ directory に anchor する行は数えない）と、BM25 または vector score の下限を越えた qmd 行だけの和集合（lexical の裾は数えない）。qmd index が無いとき、および vector 層が degrade しているときは file 粒度の場所一致だけになる。directory 一致の行は `K` 行の 1 つを占めることがあり、`M` には必ず数えるが、strong には決してならない。`S` が `K` を上回るときに、確度の高い候補が block から溢れたと読む。場所信号の出所は hooks の節にある `.reads` / `.touched` ledger、調整軸は `TASKFLOW_NOTES_PUSH` と `TASKFLOW_QMD_BIN`。note に到達する経路は 2 つだけで、他は無い: この block と、名指しした symbol を編集・調査する間の経路（`query-lsp` skill の `incoming-calls` / `outgoing-calls` で depth-1 の callers / callees を列挙し、path が repo 相対の peer だけを残して `::<name>` で `project-notes/` を Grep）。この経路は `query-lsp` が使えないとき block だけに degrade するので、query-lsp を持たない project では recall が落ちるだけで他に影響しない。後者があるのは、block が `anchors:` を file / directory 粒度でしか照合せず、触っていない file の symbol に anchor された note は block に現れえないため。

topic 層を project で有効にするには、npm package `@tobilu/qmd`（Node ≥ 22）を入れ、`_projects/<project>/project-notes/` で `qmd init`、`qmd collection add .` を実行し、`.qmd/index.yml` の collection に `ignore: ["index.md", "_archive/**"]` を足してから `qmd embed` を走らせる（GPU 推奨。Vulkan GPU で embed 中に abort する場合は環境変数 `GGML_VK_DISABLE_COOPMAT=1` が既知の対処）。`_projects/` を版管理するなら `project-notes/.qmd/` は ignore する。以降は Stop hook が index を tree に追随させ、`qmd status` が pending の document を報告する限り embed する（[hook](#hook) 参照）。全再生成（`qmd embed -f`）は自動では走らず、手動 command のままである。push は常にその時点で index 済みの範囲で動く。

#### 再利用可能な知識の同ターン保存

そのターンが再利用可能な project 知識を生んだなら、メインエージェントは同じターンのうちに `project-notes/<category>/<slug>.md` へ書き、確認は取らない。これを知らせる信号は無い — router の field も承認ステップも存在しない。判断はエージェント自身のもので、ターン末に `taskflow/prompts/notes_guidelines.md` の 2 節に照らして下す: `## What to save` が「そのターンが対象を生んだか」を決め、`## Prohibitions` は両方が当たるとき保存に優先する（このとき保存も確認もしない）。`project-notes/index.md` も同じターンで更新する。

詳細は `taskflow/prompts/notes_guidelines.md` の `## Saving` / `## What to save` / `## Prohibitions` 節を参照。

- 保存する: repo 調査、案の比較、codebase 理解メモ、テスト仕様、運用手順、backlog 候補
- 保存しない: code から導ける情報、単一セッション限りのメモ、LLM 間 handoff（`_projects/<project>/llm-handoff/` へ）、およびユーザが保存するなと言ったもの — ユーザ指示はこの規則に優先する

## ディレクトリ構造

```
_projects/
  index.md                    全プロジェクト一覧
  _state/                     セッション状態（自動管理）
  <project>/
    index.md                  プロジェクト概要
    progress.md               タスク index
    tasks/
      0_todo/                 未着手
      1_in_progress/          作業中
      2_done/                 完了（人間承認済）
    project-notes/
      index.md                4 列 index
      specs/                  仕様・設計・決定
      investigations/         調査・分析・post-mortem
      checks/                 確認項目・チェックリスト
      procedures/             人間向け手順書
      backlog/                候補・アイデア
      _archive/               役目終了
    llm-handoff/              LLM 間 handoff メモ（flat・index なし・一時的）
    _archive/                 プロジェクトレベル archive
    plans/                    plan コピー（自動・履歴保管）
    memory/                   memory コピー（自動・履歴保管）
    .locks/                   @log / progress.md 書込ロック（自動管理・通常は空）
```

`.locks/` は、書込対象ごとに 1 つの短命なサイドカーを保持する：タスクの
`@log` / `@notes` ブロックを書き込んでいる間だけ存在するもの（ファイル名は
タスクのファイル名に対応）と、`progress.md` 自体を rebuild している間だけ
存在する `progress.md.lock`。必要時に生成され、最後の writer が終了した時点で
各エントリは削除されるため、静止状態のプロジェクトではこのディレクトリは空になる。
編集・コミットしてはならない。

このロックは advisory かつ **cross-harness** である：Pi の taskflow 拡張が同じ
サイドカーパスを導出するため、2 つのハーネスは互いに直列化される — ただし
双方が protocol v2 で動作している場合に限る。また、手編集や LLM の `Edit` ツール
呼び出しは、このロックを取得できない層で書き込むためカバーされない。そうした編集が
hook の書込と競合した場合は依然として無防備である。

エントリが残留する場合（ロック存在中にタスクが削除された、writer が強制終了された等）は
`/progress check` が報告し、`scripts/clean_locks.py <project> --apply` で除去できる。
対象がまだ存在するのにクラッシュで孤児化したサイドカー — その両者が意図的に一度も
フラグしない `progress.md.lock` を含む — は代わりに、`TASKFLOW_LOCK_STALE` を過ぎた
時点で次の writer が回収する。`flock` 方式と異なり、プロセスが強制終了されても自動で
解放されるものは何もない。

## 仕組み

### 全体フロー

```
セッション開始
  │
  ├─ [SessionStart:compact hook]（auto-compaction 時のみ）─→ injection フラグをリセットし次ターンで guidelines を再注入
  │
  ├─ [UserPromptSubmit hook] ─→ state_file作成 + pj:パース + session情報 / guidelines 注入
  │
  ├─ [LLM] プロジェクト判定（プロジェクトが有効なときのみ。空のときはスキップ）─→ state_fileにプロジェクト名を書き込み
  │
  ├─ [LLM] 適用判定 ─→ progress管理が必要か判断
  │     不要 → タスク実行のみ
  │     必要 → progress.md / tasks / project-notes を読み書き
  │
  ├─ [LLM] ターン末の保存判断 ─→ そのターンが生んだ再利用可能な知識を同ターンで project-notes/ へ書く（確認なし）
  │
  ├─ タスク実行
  │     ├─ [PreToolUse:Write|Edit] project-notes/ ファイル書込 ─→ project-notes/index.md 同期ルールを注入
  │     └─ [PostToolUse:Write|Edit|NotebookEdit|Bash] ─→ (a) tasks/ 変更時に progress.md テーブルを rebuild
  │                                                       (b) 書込先パスを per-session .touched ledger に追記
  │
  └─ [Stop hooks] ─→ plan/memory コピーをアーカイブ、かつ
                     当該セッションの作業を各 touched / owning task の @log に bind。
                     summary + note↔task-link の判定は async な progress-capture
                     サブエージェントに委譲する（不在時は決定論バックストップ）
```

### hook

10 の hook スクリプトがプラグイン有効時に自動で動作する。`hooks/hooks.json` で `UserPromptSubmit` / `PreToolUse`（2 つ）/ `PostToolUse`（3 つ）/ `Stop`（2 つ）/ `SessionStart:compact` / `PreCompact` に wire されている。`notes_push.py`（毎ターンの project-notes push）は `session_init.py` が import する共有モジュールで、wire された hook ではない。加えて `note_links.py`（note↔task link のデータ層）、`qmd_index.py`（qmd index 追随の driver）、`log_lock.py`（bounded advisory write lock、protocol v2）の 3 ファイルは共有モジュールであり、単体で wire された hook ではない：`note_links.py` と `qmd_index.py` は Stop hook が import し、`log_lock.py` は Stop hook に加えて `scripts/rebuild_progress.py` も import する（後者は `progress.md` の read→write 区間を同じロックで保護する）。

**`_projects/` の解決先。** state ツリーを必要とする hook はいずれも、セッションの起動ディレクトリから祖先方向へ辿り、`_projects/_state` を持つ最も近いディレクトリを root とする（起動ディレクトリ自身も候補に含む）。該当する祖先が無ければ起動ディレクトリにフォールバックする。hook プロセスはセッションが**起動された**ディレクトリをセッションの生涯にわたって保持する（Bash ツール呼び出し中の `cd` では動かない）ため、ワークスペースのサブディレクトリで Claude Code を起動すると、従来はそのセッション全体でプラグインが no-op になっていた。現在はワークスペース既存のツリーに合流する。意図的な例外は Stop hook の 7 日 stale-marker クリーンアップだけで、その対象は起動ディレクトリに固定されたままである — サブディレクトリで起動したセッションはこの掃除を単にスキップし、次にワークスペース root で起動したセッションが実行する。

#### UserPromptSubmit: session_init.py

毎ターン実行。`_projects/_state/{session_id}.json` を管理し、`[Progress Session]` を LLM コンテキストに注入する。`_projects/` が存在しない場合は自動生成する（`_state/` とテンプレート `index.md` も同時作成）。

通常ターンではさらに `hooks/progress_digest.py` 経由で `[Project Progress: <project>]` block を注入する: `progress.md` から切り出した `## In Progress` の表 header・separator と最大 `IN_PROGRESS_ROWS_MAX`（5）行の data 行、末尾に `in-progress: <shown> of <total> rows shown`。`project_routing.md` や guidelines 全文が載るターンでは注入しない（その payload だけで harness の inline 配送予算を超えており、digest は途中で切れて届くため）。`pj:?` の discovery ターンでも注入しない。組み上げた payload 全体が予算を超えるときは `[Project Progress] In Progress digest omitted (context budget)` の 1 行に差し替わる。digest が落とした範囲（残りの In Progress 行、TODO / Completed の表）は `scripts/view_progress.py` を実行して取る。

毎ターンの `project-notes (index rows):` push block（[project-notes](#project-notes) 参照）も `hooks/notes_push.py` 経由でここが組み立てる: note の `anchors:` を、そのターンが名指し・Read（`.reads` ledger）・書込（`.touched` ledger）した path と照合した場所行、次に qmd 構造化 query（`node <bin/qmd> query … --no-rerank --json`、timeout は `TASKFLOW_QMD_TIMEOUT` 秒で既定 6、project に `project-notes/.qmd/` index があるときのみ）とプロセス内 lexical rank を合成した topic 行。qmd が期待されるのに動かない場合は topic 層を lexical rank のみとし、`[project-notes] vector layer unavailable: <reason>` を **degrade が続く限り毎ターン** 注入する。以前は session につき 1 回だったが、主な原因（qmd が必要とする環境変数の登録より前に起動された祖先を持つ process）は自然には解消せず、1 回の告知は流れてしまうため、**4 日間複数 session にわたって見逃されたことが実測された**。reason は行ごとに付くので、原因が変わったことも見える。

ガイドライン注入も担当: セッション初回ターン（および compact 後）に `progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md` の全文を注入し、以降のターンでは reminder のみを注入してトークンコストを削減する — `guidelines_reminder.md`（デフォルト）または `guidelines_reminder_manifest.md`（`TASKFLOW_GUIDELINES_REMINDER=manifest`。[設定](#taskflow_guidelines_reminder) 参照）。

##### guidelines_reminder.md のメンテナンス

`prompts/guidelines_reminder.md` は初回以降の毎ターンに注入されるキーワードリマインダー。会話冒頭で注入された全文ガイドラインへの LLM のアテンションを再活性化する設計。`prompts/guidelines_reminder_manifest.md` は `TASKFLOW_GUIDELINES_REMINDER=manifest` で選択される低コスト variant: PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE の内容は recall label に圧縮されているが、毎ターン無条件発火が要件の ROUTER・RESPONSE LEADING LINES の2行は全文のまま残し、`guidelines_reminder.md` の同じ2行と byte 一致でなければならない（`tests/test_guidelines_reminder_mode.sh` が機械検証）。

**設計原則**: 元のガイドラインから特徴的な用語（特に禁止規則、フォーマット固有パターン、権威の定義）を抽出し、対応する全文パッセージへのアテンション重みを強化する。

**メンテナンスルール**: reminder に供給するソース — 3 つのガイドライン（`progress_guidelines.md`、`notes_guidelines.md`、`tasks_guidelines.md`）＋ `project_routing.md`（ROUTER cue のソース）— のいずれかを更新したら、`guidelines_reminder.md` と `guidelines_reminder_manifest.md` の**両方**を同じコミットで必ず更新すること。削除されたルールのキーワードが残ると幻覚的な制約を引き起こし、新ルールのキーワードが欠けるとサイレントな非準拠を招く。

**キーワード選定基準**（優先順）:

1. 禁止規則（してはいけないこと）— 忘却時の違反リスクが最も高い
2. フォーマット固有パターン（frontmatter フィールド、ファイル名規約、文字数制限）
3. 権威の定義（どのフィールドがどの情報源を正とするか）

##### プロジェクトルール（rules.md）

`session_init.py` は、存在すればプロジェクト固有ルールファイル `_projects/<project>/rules.md` も注入する。ルールは taskflow プロジェクト（`pj:`）単位のスコープであり、ファイルパス単位ではない — パス/glob 単位のルールは `.claude/rules`、グローバルなルールは `CLAUDE.md` を使う。

- **プロジェクト切替時**: 全文を 1 回 *primer* として注入。
- **以降のターン**: ファイルの `##` 見出しの簡潔な manifest を recall cue（「行動前に読む」トリガー）として再掲し、全文を再注入せず低トークンコストでルールを温存する。
- **`inject_every_turn: true`**（ファイルの frontmatter）: 代わりに全文を毎ターン注入する — 常に温存されるが毎ターンのトークンコストがかかる。

ファイルは人間が編集する（モデル自己判断の書込なし）。エージェントは diff を提示し、ユーザ承認後にのみ適用する。state file の `project_rules_indexed` が切替時注入をゲートし、compaction 時にリセットされて primer が再注入される。設計の全文は `_projects/harness-taskflow/project-notes/specs/project-rules-injection.md`（開発リポジトリの設計メモ・配布物には含まない）を参照。

#### PreToolUse: notes_index_reminder.py (matcher: Write|Edit)

`_projects/<project>/project-notes/` 配下のファイル（`index.md` 自身は除く）への `Write`/`Edit` 直前に発火。`additionalContext` 経由で `[Project Notes Index Rule]` リマインダーを注入し、操作後に `project-notes/index.md` を同期するよう LLM に指示する（新規ファイルは行追加、Description/Tags 変更時は該当行更新、削除時は該当行除去）。

#### PreToolUse: notes_scan_gate.py (matcher: Grep)

検索の起点 — `path`、`path` が無ければ `cwd` — が `project-notes/` 配下（`_archive/` 含む）にあって実在する 1 ファイルではなく、かつ `pattern` に `::` を含まない `Grep` を拒否する。この形は note ルールが禁じる corpus 走査そのもの — symbol 経路の手順 3 は peer ごとに `::<name>` を grep するもので、それ以外に corpus を列挙してよい手段は無い。実在する 1 つの note の中の Grep は他の note を探さないので、そのファイルの `Read` と同じく通す。ディスク上で解決できない起点は配下全体とみなす（安全側に拒否）。相対 `path` は payload の `cwd` に対して解決し、Windows では MSYS 形式の `/c/...` を `C:/...` として読む。拒否は `permissionDecision: "deny"` で、理由としてルール本文を運ぶため、止められたエージェントは闇雲に再試行せず何が許されるかを知り、同じ検索を別の手段でやり直さないよう求められる。適用範囲: 閉じるのは `Grep` tool だけ。`Read` は決して拒否しない — 正規に到達した note（block または `::` Grep 経由）は `Read` で開かれ、hook はそのファイルの由来を判別できないため。`Glob`→`Read` と `Bash` 経由の `grep`/`rg`/`cat` は通り、配下より上を起点とする Grep（プロジェクトディレクトリ、リポジトリ直下）も通る — これを判定するとプロジェクトの通常の検索まで拒否するため。subagent 自身の呼び出しは**通らない** — `touched_capture.py` が subagent の書込を見るのと同じく、親の登録で拒否される。保証は「配下を起点とし 1 ファイルに限らない Grep tool の検索が不可能」であって「corpus 走査が不可能」ではない。運用上の注意: matcher 集合はセッション起動時に固定されるので、稼働中のセッションにこの hook を足しても効かない。

#### PostToolUse: task_rebuild_progress.py (matcher: Write|Edit|Bash)

`_projects/<project>/tasks/<status>/` 配下のファイルへの `Write`/`Edit` 直後、または当該パスを参照する `Bash` コマンド（例: `/progress start` / `approve` / `unstart` が使う `mv` — rename 単独では `Write`/`Edit` が発火しない）の直後に発火。`scripts/rebuild_progress.py` を実行して該当プロジェクトの `progress.md` テーブル領域を再生成し、手動の `/progress rebuild` なしでタスクインデックスを最新に保つ。

#### PostToolUse: touched_capture.py (matcher: Write|Edit|NotebookEdit|Bash)

すべての `Write` / `Edit` / `NotebookEdit` と、ファイルを触る `Bash`（`mv`/`cp`/`rm`、`>`/`>>` リダイレクト、`tee`、および in-place `sed -i`/`--in-place`）の直後に発火。書込先の正規化 repo-relative パスを per-session の `_projects/_state/{session_id}.touched` ledger（append-only、lock-free）に追記する。この ledger が、Stop の capture hook が「このセッションが実際に触った task」を判定する入力になる。jsonl スキャンや git diff ではなく **このセッションの tool 書込** を観測するため、無関係な task の誤 stamp を避けられる。サブエージェント / fork の内部書込は親の `session_id` で発火するため、自動的に親の ledger に入る — ただしその帰結として除外が 2 つある。1 つめは、`_projects/_state/` 配下への書込は落とすため、capture サブエージェント自身の判定 sidecar が、それが供給する ledger に現れることはない。2 つめはワークスペース直下の `tmp/` で、この tree は scratch であり、mode-orchestrator の run が自分の成果物パスを数百行積んでラウンドの実編集を埋めてしまうため落とす（入れ子の `sub/tmp/` は通常の作業として記録される）。heredoc の body はコマンド解析の前に除去されるため、body 内のテキストがリダイレクトと誤認されることはない（body 自身が行う書込も記録されない。これは `python -c open()` と同じく従来から明示受容している捕捉ギャップ。`sed -i`/`--in-place`（heredoc body 内を除く）は 2026-08-20 時点で認識対象であり、`$f` のような未展開のシェルメタ文字を含む operand はスキップされる）。未終端の heredoc は、コマンドを記述どおり解析する従来動作にフォールバックする。

#### PostToolUse: read_capture.py (matcher: Read)

すべての `Read` の直後に発火。読んだファイルの repo 相対 path を session ごとの `_projects/_state/{session_id}.reads` ledger に追記し、追記のたびに末尾 200 行へ切り詰める。`.touched` とは意図的に別ファイルにしてある: `.touched` の生の行数は Stop hook の round cursor なので、Read をそこに記録すると読むだけのターンで round が開いてしまう。`.reads` の末尾（`.touched` の末尾と prompt 中の path と併せて）が project-notes push の場所信号になる。除外規則は同じ（`_projects/_state/`、`tmp/`、state ツリー外の path）。

#### PreCompact: precompact_flush.py

Claude Code が会話を要約する直前に発火する（matcher を付けていないため、手動 `/compact` と auto-compaction の両方で発火）。compaction 専用の機構ではなく、**Stop hook と同一のラウンド計算の 2 番目の呼出点**である。進行中ラウンドの `.touched` ledger スライスを読み、エージェント自身が既に `@log` を書いた task を除外し、それでも未記録の作業が残っていれば各 task の `@log` に `(auto) unflushed at compaction; summary pending (r<N>)` を追記したうえで、その per-task の進捗を逐語で要約に残すよう要約器へ指示する plain text 1 行を出力する。`N` は次の `Stop` が commit するラウンド番号なので、同一ラウンド内で 2 回 compaction が起きても 1 行に収束する。

未記録の作業が無ければ **何も出力せず** 終了する。common case を完全に無音に保つことは、Claude Code の precomputed compaction 再利用を壊さないためにも必要である。`{session_id}.bind` sidecar には書き込まない（唯一の writer は `Stop` hook）ため、カーソルは動かず、compaction 直後の `Stop` は通常どおり自分のラウンドを形成する。

#### SessionStart: session_compact_reset.py (matcher: compact)

Claude Code が会話を auto-compaction した際に発火。compaction では `session_init.py` が注入した `additionalContext` が失われるため、state file の injection フラグ（`rules_loaded`, `indexed_project`, `guidelines_loaded`, `project_rules_indexed`）をリセットする（他のフィールドは保持）。次の `UserPromptSubmit` ターンで `static_rules`・プロジェクトインデックス・全文ガイドライン・`rules.md` primer が再注入される。

#### Stop: session_sync.py

セッション終了時に実行。直近 10 分以内に更新された plan/memory ファイルをプロジェクトディレクトリにコピーする。

#### Stop: session_progress_capture.py

セッション終了時に `session_sync.py` と並列で実行。当該セッションの作業を、各 owning task の append-only `@log` ブロックに `- <ISO8601> [s:<sid>]: <summary>` 行として bind する。owning task の判定には `.touched` ledger（上記）と `[tasks:]` exec-binding carry（下記）を用いる。

bind の単位はセッションではなく **ラウンド**。`.touched` ledger は書込イベント 1 件につき 1 行の append-only なので、hook はそこにカーソルを保持する。各 `Stop` は前回 commit したラウンド以降に書かれたスライスだけを見て、それがそのラウンドの active task 集合を決める — 直接書かれた task、そのスライスで書かれた `project-notes/` 成果物の owning task（reverse index で解決）、およびそのターンの `[tasks:]` carry。そのラウンドで既にエージェント自身が `@log` を書いた task は集合から除外されるため、ガイドラインどおりのターンでは spawn は一切起きない — hook は記録されないまま終わるラウンドのための安全網である。したがって長いセッションでは、task ごとに「セッション全体で 1 行」ではなく「ラウンドごとに 1 行」が積み上がる。

task の解決はセッション自身のプロジェクトに**限定されない**。ledger の各行は `_projects/<project>/` プレフィックスを自ら持つため、同一リポジトリ内の別プロジェクトの task をセッションが編集していた場合も、その*プロジェクトの root* で解決・bind される — 従来こうした書込みは無言で捨てられていた。プロジェクトとして受理されるのは `_projects/<name>/tasks/` が存在する名前だけで、これにより `_projects/_state/` サイドカーディレクトリがプロジェクト扱いされることはない。結果として task は、round 状態でも、capture サブエージェントのコンテキスト（`project_roots` マップも併せて渡される）でも、hook の報告行でも `<project>/<basename>` で識別される。

owner 判定 — active task ごとの 1 行 summary と、新規書込された `project-notes/` 成果物の note↔task link — は async な `taskflow:progress-capture` サブエージェントに委譲する: hook は `capture.status=requested` を commit し（そのラウンドの task 集合を凍結する）、サブエージェント起動を促す block を 1 回返す。サブエージェントは `{session_id}.r{N}.capture` JSON サイドカーを書き（`N` は spawn 元のラウンド番号。hook がファイル名に刻む）、後続の `Stop` がそれを決定論的に apply する（`@log` summary は `append_auto_binding`、note link は `append_note_link`）。30 秒の expiry 内にサイドカーが現れなければ、決定論バックストップが、そのラウンドでまだ 1 行も落ちていない task を placeholder-bind する — ただし粒度の異なる 2 つの例外がある。ラウンド単位の例外: `[tasks:]` carry **だけ**で開いたラウンド、すなわちそのラウンド自身のスライスに task 書込も `project-notes/` 書込も無いラウンドは、まるごと skip される。何も触っていないラウンドについて「touched」は事実に反するからである（carry と実スライス書込が混在するラウンドはこれに該当せず、従来どおり placeholder-bind される）。task 単位の例外: placeholder ステップは、現在 `tasks/2_done/` 配下にある task も skip する（backstop 実行時点で current index から動的に判定）。placeholder ステップより前に走る `referenced` note-owner over-bind は task の status フォルダの影響を受けず、`2_done` の task にも通常どおり bind する。carry のみのラウンドでも、サイドカーが届けば実 summary は従来どおり apply される。expiry の**後**に完了したサブエージェントも無駄にはならず、次ラウンドに先を越されても構わない: `.bind` が直近 3 ラウンド分の凍結された task/note 集合を保持するため、サイドカーは常に**自身のファイル名が示すラウンド**の集合と照合され、placeholder の上に summary と note link を apply する。lifecycle を閉じるのは現ラウンドのサイドカーのみで、保持窓より古いラウンドを名乗るサイドカーは適用せず破棄し `round-mismatch` として 1 回だけ報告する — 読めない（書きかけ / 非 JSON の）サイドカーも、そのラウンドが保持窓を出た時点で同じ処分を受ける（窓内にある間は無言でリトライされる）。placeholder 行には `(r<N>)` のラウンドタグが付き、これが冪等キーも兼ねる — 同一エントリの再 apply が行を重複させることはない。サブエージェントにはもう 1 つ判断すべきことがある: ある task の `## Next Steps` が、その task に summary が落ち続けたラウンドをまたいで書き換えられないままなら、hook はその task を request の context block に列挙し、置換案の提示を求める。hook がその提案を task 本文へ書き込むことはない — genesis task の提案で既に使っている同じ Stop ブロックに折り込む（新しい通知経路は無い）。ただし独自の見出し「Proposed Next Steps rewrites」の下に `(next-steps)` プレフィックス付きで表示され、同ブロック内の genesis `(proposed)` task 提案とは見分けが付く形になる。適用するかどうかはあなたに委ねる。提示は 1 セッション・1 task につき最大 1 回。round / lifecycle 状態（カーソル・ラウンド番号・task ごとの行数・直近 3 ラウンドの task/note 集合、およびこの提案の根拠となる task ごとの `## Next Steps` digest と滞留カウンタ）は `{session_id}.bind` サイドカーに保持する（state JSON とは分離し、他フックの並行書換による clobber を防ぐ）。旧 `{session_id}.captured` マーカーは legacy で、7 日クリーンアップで掃除されるのみ。設計は `_projects/harness-taskflow/project-notes/specs/exec-binding.md`（開発リポジトリの設計メモ。プラグインには同梱されない）と `project-notes/specs/note-task-link.md` を参照。

##### exec-binding（`[tasks:]` carry）

セッションの task 作業の結果が、その task 自身の `tasks/<status>/*.md` の **外** に落ちる場合（execution-by-reference — 例: task や handoff を読んで結果を別所に書く）、エージェントは応答の先頭行に `[tasks: a.md b.md]` で owning task のファイル名を列挙する。`session_progress_capture.py` はこの carry を読み、state の `exec_bind` 配列に union-merge し、各 owning task の `@log` に bind する。これにより `tasks/` を一切編集しなくても作業が記録される。task ファイルを直接編集した場合は `[tasks:]` は不要（PostToolUse の `.touched` capture が記録する）。名前が現プロジェクトのどの task md にも一致しない場合、hook は黙って無視せず 1 回だけ `exec-skip(unresolved)` として報告する（綴りを確認すること。carry の解決対象はアクティブなプロジェクトのみ）。

##### note↔task link（`@notes` block）

セッションが永続的な `project-notes/` 成果物を書くと、progress-capture サブエージェントがそれを owning task に対応付け、hook が link を **task 側** に記録する — task ファイル内の自動管理ブロック `<!-- @notes:begin/end -->` に project-relative な note パスを列挙する（`note_links.py`）。この link は、プロジェクトディレクトリの rename / move に task ファイルと共に追随する。stale なエントリ（ファイルが消えた note）は reverse index 構築時にスキップされる。

##### qmd index 追随（`qmd_index.py`）

Stop hook は `hooks/qmd_index.py` 経由で、各 project の qmd index をその `project-notes/` tree に追随させる。経路を開く gate は 2 つで、両方が必要: `TASKFLOW_NOTES_PUSH` が `auto`（既定）であること、および project に `project-notes/.qmd/` directory があること。

index の 2 つの半分は、別々の条件で駆動する。lexical 側の `node <bin/qmd> update` は、`UPDATE_TIMEOUT_S` = 5 秒固定の上限つきで同期実行し（超過したら捨てて Stop は続行）、その round の ledger slice が `project-notes/` 成果物を書いた project でのみ走る。vector 側の `node <bin/qmd> embed` は、その round の書込ではなく index が tree からどれだけ遅れているかで決まる: まず `node <bin/qmd> status` を probe し、その `Pending:` が vector の欠落・不完全な document 数であり、pending が 0 なら embed は起動しない（`current`）。note を何も書かなかった round でも、pending がある限り embed は起動する。embed は detach 起動するので hook より長生きし、session 終了を待たせない。実行は GPU — qmd 自身の既定 — で始まり、`--no-gpu` には後述の escalation 経由でのみ到達する（GPU の run が何も解消しなかったと観測された後）。したがって GPU backend が embed できない machine でも、vector が無いままにはならない。GPU を先にする根拠は実測差である: 82 document / 789 chunk が GPU で 93.9 秒、CPU で約 155 分。この escalation は pending 数が動かなかった Stop を必要とするので、そういう machine では、書込が続いている project は note を書いた Stop ごとに無駄な GPU 起動を 1 回払い、静かな Stop が来て初めて CPU の run が始まる。そして [project-notes](#project-notes) の setup 手順が挙げている `GGML_VK_DISABLE_COOPMAT=1` は、手動の初回 build のためだけでなく、この経路にとっても load-bearing になった。受容したコストは競合である — detach した GPU embed は、同じ embedding model を GPU で走らせる毎ターンの `vec:` query（prompt 注入経路）と重なりうる。この重なりは未計測であり、超過したターンは失敗にはならず lexical 層へ degrade して告知される。`qmd embed -f` はここから到達しない。collection 全体の再生成は手動 command のままである。

probe には qmd 自身の local config — `project-notes/.qmd/index.yaml` または `index.yml` — が要る。これが無いと qmd の設定探索が上位へ walk し、祖先または global の index をこの project の数値として答えてしまうためである。`.qmd/` はあるが該当 file が無い project は probe しない（`no-local-index`）。probe 1 回の上限は `STATUS_TIMEOUT_S` = 3 秒。加えて 1 回の Stop 内の全 probe が `FOLLOW_BUDGET_S` = 4 秒の wall-clock 予算を 1 つ共有するので、project 数が Stop の latency を掛け算することはない。予算を使い切った後に到達した project は probe しない（`no-budget`）。起動できない・非 0 で終了した・timeout した・parser が解さない出力だった probe も数値を得られない（`status-failed`）。probe の実測は 80 document / 8.4 MB の index に対し 0.26 秒。

遅れ具合が不明のまま終わった枝では — `no-local-index` / `no-budget` / `status-failed` のいずれでも — 起動条件はその round の note 書込へ fallback する: その round が当該 project の `project-notes/` に何も書いていなければ見送り、書いていれば起動まで進む。したがって `status-failed` と `no-budget` が stderr に出るのは、その round に note を書いていない project に限られる。失敗は全ての枝で skip であり、Stop を失敗させることはない。

blast radius を抑える cap が 2 つある。**1 Stop につき 1 embed**: その session が触れた全 project を、session 自身の project を先頭、残りを名前順に並べて提示し、Stop 全体を決着させる最初の status で loop を終える — `not-auto` と `no-bin`（次の project でも同じ答えになる env 全体の判定）、`locked`（既に embed が走っている）、`embedding` / `embedding-cpu` と `embed-failed`（この Stop の 1 回の起動）。`futile-retry` は意図的に loop を終えない: 何も走っておらず、この Stop の起動枠も未使用なので、後続の project がまだ使いうる。**1 project につき同時 1 embed**: `project-notes/.qmd/embed.lock` で担保する。これは起動した pid、その起動が解消するはずだった pending 数、その起動が使った mode（`gpu` / `cpu`）、および起動した session tag / round を持つ JSON file である。lock が止めるのは **pid の生存だけ** — 別 session のものを含め embed が走っている場合である。記録された session tag / round は診断用であり、何も gate しない。同じ file をもう 1 つの gate が読む: 記録された pid が既に消えており、かつ pending 数がその起動時の値とちょうど同じなら、その run は何も解消しなかったということであり、escalation がちょうど 1 段だけ続く — `gpu` の run は CPU で再試行し（`embedding-cpu`）、`cpu` の run は見送り（`futile-retry`）、`mode` が無いか未知の値の lock — 例えば key が存在しなかった頃のもの — は GPU で再試行する。pending が減った・増えた・この Stop が読めなかった場合は、lock の mode が何であれ GPU で起動する: `mode` が記録するのは「その run がどの device を要求したか」であって、machine の能力ではなく、qmd が実際に走らせた device とも限らない（継承された `QMD_FORCE_CPU` や `QMD_LLAMA_GPU=false` は flag を上書きし、escalation の収束が 1 run 延びる）。GPU が復旧したことを知らせる信号は存在しないので、CPU を固定すると直った machine が永久に遅い側へ張り付く。代償は CPU batch 1 本あたり無駄な GPU 起動 1 回で、次の Stop が取り返す。file は決して削除しない — 生存判定は pid から行うので、残った `embed.lock` は自然に解消し、cleanup 経路を必要としない。lock が止めている場合でも `update` は走る。安価であり、CPU fallback では分単位になる embed の後ろに lexical 側を待たせると、その間ずっと最新の note がその層から漏れるためである。

追随は stderr に `[progress capture] qmd-follow(<project>): <status> [s:<sid>]` の形で報告する。起動・阻止・失敗は 1 行を出す: `embedding`、`embedding-cpu`、`locked`、`futile-retry`、`no-bin`、`status-failed`、`no-budget`、`embed-failed`。CPU fallback が loud なのは固有の理由による — 黙って変わる mode は誰も気付かない。残りは黙る — `not-auto` / `no-qmd` / `no-local-index` はその tree がこの module の対象外であることを意味し、`current` は既に追いついている index に対する通常の答えなので、どちらも出せば全 session の全 Stop に 1 行付くことになる。

## 既知の問題

- **state file の競合**: 複数フック（`session_init.py`, `session_compact_reset.py`）が同一の `_projects/_state/{session_id}.json` をロックなしで読み書きする。実際にはトリガーイベント（`UserPromptSubmit` と `SessionStart:compact`）が同時発火しないためデータ損失は観測されていない。将来のリリースで atomic write または advisory lock の導入を予定。（capture の round 状態と touched ledger は、この種の clobber を避けるため `.bind` / `.touched` / `.capture` の別サイドカーに意図的に分離しており、`@log` / `@notes` 書込は `log_lock.py` の bounded advisory lock で直列化される。）
