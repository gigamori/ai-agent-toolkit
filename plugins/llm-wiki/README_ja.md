# llm-wiki

**LLM が維持管理する wiki** を実現する Claude Code プラグイン。ソースを Markdown ページに ingest し、それらのページに接地して質問に答え、wiki グラフを lint する。プラグインは**不変エンジン**（D1）であり、ingest / query / lint の手続きと、per-wiki repo の初期化元となる wiki 契約の**スキーマテンプレート**を同梱する。wiki の契約自体は書き換えない。各 wiki は独立した per-wiki repo で、自身の schema / index / log / raw / pages を保持し、それらが co-evolve する。

[English README is here](README.md)

> **はじめての方へ** — まずは図解つきのタスク志向ガイド **[ユーザーガイド](USER_GUIDE_ja.md)** から。この README は全リファレンス（コマンド・設定・安全モデル・設計の根拠）です。

## これが解決する問題

メモや決定はチャット・コマンド出力・セッションログに散在する。llm-wiki はそれらを不変の `raw/` アーティファクトに正規化し、コードで強制される安全境界の下で LLM に wiki ページの執筆・更新を行わせる：untrusted なソース読取とページ書込を分離し、すべての書込を allowlist ゲートに通し、ingest 全体を単一のファイルジャーナル・トランザクションとして走らせる（失敗時は wiki を ingest 前の状態にロールバック）。

## インストール（Claude Code）

### plugin marketplace 経由（推奨）

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install llm-wiki@ai-agent-toolkit
```

### ローカル（開発・テスト）

```bash
claude --plugin-dir ./plugins/llm-wiki
```

プラグインの決定的エンジンは path-import される Python パッケージ（`llmwiki/`、install 不要）で、`bin/` 配下の 3 つの CLI entrypoint を `uv run` で起動して駆動する（各 entrypoint は PEP 723 で自身の依存を宣言する）：

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki <verb> ...        # dep-free: resolve-root scan-pages search file declare promote-check promote lint init marker-detect ingest-apply floor-check reindex
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest ... # duckdb:   ingest {begin|plan-fanout|finish|apply-finish|abort|enumerate|session-plan|project-batch|project-batch-cleanup}
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-view view --serve # markdown: ローカル HTML ビューア
```

commands / skills / agents / hook はこれらの entrypoint を呼ぶだけの薄い shim。dep-free な読取経路（`bin/llmwiki`）は `duckdb` も `markdown` も巻き込まない。プラグインのロードに別途 `init` ステップは不要。`UserPromptSubmit` hook はプラグインを有効化した時点で起動する。

## wiki の初期化

wiki は **`/wiki-init`** で初期化する。選択した scope に、wiki を**プレーンなディレクトリ**（git 非依存。エンジンは git を一切呼ばない）として作成する。scope は対話的に選ぶ：

- **taskflow 有効かつ project 割当あり** — **active pj**（`_projects/<project>/wiki/`）／ **workspace**（`<workspace-root>/_llm-wiki/`）／ **path 入力** から選ぶ。
- **taskflow 無効または project 未割当** — **project を選ぶ**（`$TASKFLOW_PROJECT_ROOTS`、無ければ `_projects/` を走査）／ **workspace** ／ **path 入力**。
- active 以外の project を対象にする場合は `--root <path>` を渡す（その project は選択肢には追加しない）。

```
/wiki-init                       # 対話的に scope を選択
/wiki-init --root ./path/to/wiki # 対象 root を明示し選択を省略
```

wiki はプレーンなディレクトリであり、エンジンは git を一切呼ばない。同梱の `<wiki-root>/.gitignore` は、あなた自身が wiki を versioning する場合に、周囲の親 repo が wiki の churn を追跡しないようにする。

wiki はプラグインの `templates/` から初期化される：

- `.llmwiki` — wiki **marker**（D8）：`{ version, schema: SCHEMA.md }`。その存在がディレクトリを wiki root として標識する。検出専用で config は持たない。
- `SCHEMA.md` — wiki **契約**：規約 prose ＋ `config` と `doc_type_profiles` を持つ YAML frontmatter（8 つの doc type を全て seed、加えて必須の `default`）。
- `index.md` — content-oriented なカタログ seed。
- `log.md` — append-only ログ seed。grep 解析可能な `## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>` prefix 規約。
- `raw/` — 不変・redaction 済みのソースアーティファクト（id は content-hash。LLM は読むのみ）。redaction は Windows drive-letter パス・UNC パス・POSIX system/home root・`~/...` などローカルパスのパターンと secret 様トークンをマスクする。URL（`https://...`）やパスを伴わない裸の `~` はマスクされない。
- `wiki/` — LLM が執筆するページ。`wiki/` は source tier、`wiki/derived/` は未昇格の synthesis。

## active な wiki の解決

操作は active な wiki を**存在ベース**で上から解決する：**prompt `--root` > pj（`_projects/<project>/wiki/`）> workspace（`_llm-wiki/`）> CWD `.llmwiki` > child（`.llmwiki` を持つ直下の子ディレクトリがちょうど 1 つのときだけ）**。child scope は最後の手段で、深さ 1 限定・再帰なし：wiki フォルダの**親**を開いてしまう頻出の事故を救済する。marker 付きの子が 2 つ以上あれば曖昧なので、どれかを黙って選ばず解決しない（fail-closed）。pj scope は taskflow から一方向に読み、`$TASKFLOW_PROJECT_ROOTS` で解決する：marker hook がそのターンの `session_id` を渡すため、pj scope は **このセッション自身の** `_projects/_state/<session_id>.json` の `project` field を優先して読む — 別プロジェクトの 2 セッションを同時に走らせても互いの wiki を取り違えない。そのファイルが無い場合は最新の `_projects/_state/*.json`（mtime）へフォールバックし、state file が一切無ければ pj scope は綺麗にスキップする。（CLI はセッション文脈を持たないため `resolve-root` は mtime-latest 動作のまま。）解決が CWD だけに依存しなくなったため、`cd` の無い **VSCode 拡張でも動作する**。marker hook は毎ターン `active wiki: <root> (scope: pj|workspace|cwd|child)` を表示し、解決された wiki が常に可視になる。

## 使い方

典型的なセッション：

1. **wiki に入る。** 解決は自動（上記「active な wiki の解決」参照）— wiki root へ `cd` するか、pj/workspace scope に任せるか、`--root <path>` を渡す。scope hook はそのターンに `wiki-active`、`active wiki:` 行、そして `[wiki:on]` の leading-line 指示（taskflow の `[pj:…]` と同様）を注入する。**pj** scope で解決した場合は wiki↔taskflow の棲み分けガイド（恒久・横断知識 → wiki／タスク遂行文脈・進捗 → taskflow）と filing 提案規範も加わる。何も解決しなければプラグインは不可視のまま。

   **セッション単位でトグル — `wiki:on` / `wiki:off`。** wiki が解決されている限り既定は on。プロンプトの任意の位置に `wiki:off` を含めると、現在のセッションだけ静かにできる：hook は `wiki-active` / filing の注入を抑止し、最小限の `[wiki:off]` 通知のみを注入する（Claude は返答の冒頭に `[wiki:off]` を出し、wiki に触れない）。`wiki:on` で戻す。状態は解決した wiki root 直下の per-session marker（`.llmwiki.toggle.d/<session_id>.off`、存在 = off）なので、**セッション内では sticky**、**新しいセッションは on** で始まる。*恒久的な* off は代わりに wiki の `SCHEMA.md` の `activation_scope` を使う。wiki が一切解決されない場合、`wiki:on|off` は無視される（何も注入しない — pj 未割当と同型）。

2. **何かを入れる。** コマンドは**対象**で命名されている — ドキュメントか、今している会話か、他セッションのログか：

   ```
   /wiki-ingest-docs ./docs/rfc-routing.md       # 3rd-party ドキュメント（FE-B → source tier）
   /wiki-ingest-docs ./docs/rfc.md external=https://example.com/rfc   # citation 用の permalink を付ける
   /wiki-file                                    # 今のこの会話（FE-B' → derived tier、doc_type=transcript に pin）
   /wiki-ingest-sessions --workspace             # 他セッションのログを scope 指定で
   ```

   `/wiki-ingest-docs` の引数は**クォートした glob** や**ディレクトリ**も指定できる — driver が（シェルではなく）Python で展開し、**1 ファイル 1 トランザクション**で ingest する：

   ```
   /wiki-ingest-docs "./docs/**/*.md"            # クォートした glob：Python で展開、wiki 内部パスを除外、1 ファイル 1 トランザクション
   /wiki-ingest-docs ./docs/                     # ディレクトリ：./docs/**/* として展開し、テキスト系 allowlist に限定
   ```

   ディレクトリの場合はテキスト系 allowlist（`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`）のみ拾い、非テキスト（例：画像）はスキップする。バッチでは 1 ファイルの失敗は**そのファイルだけ**ロールバックして続行し、末尾に `N total / M succeeded / K failed / S dedup-skipped` のサマリを報告する。0 件マッチはエラー。

   何かが書き込まれる前に、解決値の一行宣言（`[wiki] write_mode = explicit (default)` …）が出る。既定の `write_mode=explicit` では Stage 2 のページ適用前に確認する。ingest 全体は（ファイル単位の）単一のファイルジャーナル・トランザクションなので、失敗や却下時は wiki が ingest 前の状態にロールバックする。

3. **質問する。** 自然言語で尋ねるだけ — 例：*「retry backoff について何を決めたっけ？」*。`wiki-query` skill が自動起動し、`wiki/` と `wiki/derived/` の両方を読み、各 claim をページパスで citation する（パスが source / derived tier を示す）。これは read-only。

4. **会話を filing（任意・明示）。** query 自体は書き込まない。会話が生んだものを残す手段は 2 つある：

   ```
   /wiki-file                          # この会話を filing
   /wiki-file 最後の回答だけ             # 最後の回答に絞る
   /wiki-file retry-policy の議論だけ    # トピックで絞る
   ```

   `/wiki-file` はセッション ID もパスも要らない：source を実行中セッションに固定し、FE-B' パイプラインを pin し、自分の起動ターンの手前で投影を打ち切る（コマンドは指示であって会話内容ではない）。後続の自由文は *どのターンを* filing するかを絞るだけで、パイプラインは切り替えない。再実行は差分のみ：turn ledger が前回 filing 済みのターンを落とす。

   あるいは単に依頼してもよい — 例：*「それをページとして filing して」* — と `wiki/derived/` 配下に derived synthesis として着地する。

   （セッションが `wiki:off` の間は filing も抑止される。先に `wiki:on` で再有効化すること。`wiki:on|off` は文字列先頭または空白の直後でのみ発火し、case-insensitive なので token の途中にはマッチしない。）

5. **Lint。** `/wiki-lint` がグラフ / index 検査と transcript の decision floor を実行し、優先順位付きの「next questions」リストを返す。read-only。

6. **wiki を閲覧する。** `/wiki-view` がローカル HTML ビューア（`http://127.0.0.1:17330/`）を起動し、wiki の `wiki/` + `wiki/derived/` ページをレンダリング（サニタイズ済・CSP 付与・loopback Host のみ）して `[[wikilinks]]` をクリックで辿れるようにする。read-only。別 viewer がポートを保持中は起動を拒否する。停止は専用の **`/wiki-view-stop`** — ポート 17330 をクロスプラットフォームに解放する停止スキル（`/wiki-view` 節参照）。

7. **Promote。** `wiki/derived/` のページが source tier に値する時：`/wiki-promote wiki/derived/retry-policy.md`。明示的な承認と contamination チェックの後、`wiki/retry-policy.md` へ move し inbound link を rewrite する。derived→source の唯一の経路。

**回復（recovery）。** ingest が中断（プロセスが途中で kill）されると `.llmwiki.lock` と `.llmwiki.txn.d/` のジャーナルディレクトリが残ることがある。**単独の** stale lock — 死んだプロセスが残し、進行中トランザクションが無い（ジャーナルも sidecar も無い）もの — は次の ingest で自動回収される（記録された所有 pid の生存を確認し、疑わしきは held 扱いの fail-closed なので稼働中の ingest は決して奪わない）。ジャーナルや sidecar を伴う lock（中断されたトランザクション）は自動回収され**ない** — `abort` verb で明示的に回復する。`abort` は ingest 前の状態へロールバックし（ジャーナルを再生して orphan な `raw/` 生成物を除去し）lock を解放する。トランザクションの sidecar が書かれる前にクラッシュした場合でも回復する（sidecar だけでなくジャーナル / lock を手掛かりにする）：

```bash
uv run --script ${CLAUDE_PLUGIN_ROOT}/bin/llmwiki-ingest ingest abort <wiki-root>
```

## 操作

操作は active wiki を multi-scope（prompt>pj>workspace>cwd>child）で解決するため、wiki へ `cd` せずに動作する。特定の wiki を明示指定するには `--root <path>` を渡す。

全 CLI entrypoint は起動時に stdio を UTF-8 に固定する（stdin は strict — 破損入力は fail-fast、stdout/stderr は `errors="replace"` — 報告は決して crash しない）。ホスト locale や `PYTHONIOENCODING` より優先される。これは Windows で重要で、pipe 接続された Python の stdio は既定で ANSI コードページ（日本語環境では cp932）となり、write verb（`file`・`ingest-apply` は STDIN からページ内容を読む）を流れる UTF-8 ページ内容を拒否・破損させていた。cp932 強制 subprocess での非BMP round-trip contract テストで固定済み。

### scope 検出

`activation_scope: scoped` は `UserPromptSubmit` hook（`hooks/wiki_marker_inject.py`）として実装される：毎ターン active wiki を multi-scope（prompt>pj>workspace>cwd>child、`wiki_root_resolver` 経由）で解決する。この時そのターンの `session_id` を渡すため、pj scope はこのセッション自身の state file を優先して読む（上記「active な wiki の解決」参照）。解決できれば `wiki-active` コンテキスト（解決した root と scope を含む。CWD が wiki root でなくても — 例えば VSCode 拡張 — wiki が可視になる）、`[wiki:on]` の leading-line 指示、そして pj scope 限定で wiki↔taskflow の棲み分け／filing ガイドを注入する。どの scope でも解決できなければ silent に exit し何も注入しない（wiki の外ではプラグインは不可視）。注入されるコンテキストと `wiki-query` skill の description が query を自動起動させる。書込を伴う操作は明示的コマンドで、hook には依存しない。

hook は `wiki:on|off` トグルも扱う：プロンプト中の `wiki:on`/`wiki:off` マーカーが per-session フラグを設定する（解決した wiki root 直下に `.llmwiki.lock` / `.cc-turn-ledger.jsonl` と並べて `.llmwiki.toggle.d/<session_id>.off`、存在 = off として保持。`llmwiki/core/wiki_toggle.py` が管理し、best-effort で放置セッションを mtime prune する）。off の間は `wiki-active` / filing ブロック全体を抑止し、最小限の `[wiki:off]` 通知のみ注入する。トグルは wiki が解決された時のみ効き（それ以外は何も注入しない）、セッション sticky で既定 on（新しいセッションは on で始まる）。トグルディレクトリは ingest から強制除外される（self-ingestion guard の `.llmwiki.toggle.d`）。`.llmwiki.txn.d` と同様。

### ドキュメントの Ingest — `/wiki-ingest-docs <path-or-glob> [doc_type=...] [external=...]`

書込を伴う 3 コマンドは、パイプラインの重さではなく**対象**で命名されている：`/wiki-ingest-docs` はドキュメント、`/wiki-file` は今している会話、`/wiki-ingest-sessions` は他セッションのログを取る。3 つとも同じ 2 段 `extract → apply` core を単一のファイルジャーナル・トランザクション内で回す。

`/wiki-ingest-docs` は 3rd-party ソース（FE-B）を ingest する。引数は単一ファイルのほか、**クォートした glob**（`"./docs/**/*.md"`）や**ディレクトリ**（`./docs/`）も指定できる：driver が（シェルではなく）Python で展開し、wiki 内部パスを強制除外し、ディレクトリの場合はテキスト系 allowlist（`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`）に限定する。glob/ディレクトリは**1 ファイル 1 トランザクション**で ingest し、1 ファイルの失敗はそのファイルだけロールバックして続行、末尾に `N total / M succeeded / K failed / S dedup-skipped` のサマリを報告する（0 件マッチはエラー）。

3 つとも多段 orchestration（`begin` → Stage 1 subagent → Stage 2 subagent → `apply-finish`）なので、**能力の高いモデル**で実行すること：軽量/最小モデルは Stage 2 apply dispatch を取りこぼしたり 最後の `apply-finish` を省いたりしがちで、トランザクションが **open** のまま残る（`.llmwiki.lock` / `.llmwiki.txn` が残存しページ未生成 — `abort` verb で解消。上記「回復（recovery）」節参照）。

- **Stage 1（extract）** — `wiki-ingest-extract` subagent が redaction 済み・untrusted の raw ソースを**構造的に書込ツールなし**で読み、提案編集のみを出力する。
- **Stage 2（apply）** — `wiki-ingest-apply` subagent が**構造的に書込ツールなし**でページ更新を執筆し、page manifest として返す。orchestrator がそれらの manifest を allowlist write ツール（`llmwiki/write/write_tool.py`）に複合 `apply-finish` verb 経由で通す。同ツールは書込先を `wiki/`・`wiki/derived/` に限定し、`SCHEMA.md` / `.llmwiki` / `raw/` / 絶対パス / traversal を拒否し、budget でゲートする。touch ページが `apply_fanout_k` を超えると Stage 2 は per-cluster の apply worker に fan-out する。その後 `apply-finish` が各 cluster の manifest を適用し、index / log / commit を join 後に中央集約する。提案された touch ページ集合の総数はまず `max_count` でゲートされる：これを超える ingest は fan-out せず human gate へエスカレートするため、per-worker の書込 budget が cluster 数だけ暗黙に乗算されることはない。fan-out 時、各 cluster の worker はコード算出の絶対 manifest パス（`plan-fanout` が返す `manifest_paths`）も受け取るため、temp ファイルパスを自分で再構成する必要がない。

`begin` は `.jsonl` ソースに対し fail-closed：`--kind` が省略または `auto` で、ソースパスが `.jsonl` で終わる場合、`begin` はそれを（lock も書込も一切行わず）ingest 拒否する — session log を暗黙に plain text 扱いしない。このゲートは docs 掃引自身を守る：テキスト系 allowlist は `.jsonl` を含んだままなので `/wiki-ingest-docs ./docs/` はツリー内のセッションログを列挙しうる。拒否されたファイルはサマリで `failed` と数えられ実行は続行する。よって `/wiki-ingest-docs` は `--kind` を一切渡さない（明示 `--kind=fe_b` は `auto` と同じ origin に解決されるため、機能上の利得はゼロでゲート無効化の効果しかない）。`/wiki-file` と `/wiki-ingest-sessions` は固定の `--kind=fe_b_prime` を渡す。本物の `.jsonl` DATA ファイルを ingest したい operator は、直接 CLI 呼び出しで `--kind=fe_b` を手で渡す。

### この会話を filing — `/wiki-file [絞り込みの自由文]`

**実行中セッション**を derived synthesis として filing する。パスもセッション ID も不要：source は現 sid に固定、パイプラインは FE-B' に固定、投影は自分の起動ターンの手前で打ち切られる — コマンドは wiki への指示であって会話内容ではないため、それ以降の narration ごと除外される（直前ターンの回答は filing される。捕捉されないのはこの実行自身の報告だけで、cutoff 以降の残すべき内容は次回の実行が拾う）。

コマンド後の自由文は **どのターンを filing するかを絞る**（`/wiki-file 最後の回答だけ`、`/wiki-file retry-policy の議論だけ`）— パイプラインは切り替えない。絞り込みは構造的に削除専用：driver が渡された各ターンの content hash を再計算し、削除ではなく編集された entry があれば ingest を拒否するため、モデルはこの経路でターンを書き換え・要約・捏造できない。絞り落としたターンは filing されないので ledger にも入らず、後日の `/wiki-file` で改めて filing できる。

再実行は incremental：turn ledger が前回所有済みのターンを落とし、ledger-skipped 数が報告されるため、incremental な再実行が無音の no-op になることはない。

cc-log（FE-B'）入力は `doc_type=transcript` に pin され、決定的な decision floor（`llmwiki/ingest/transcript_floor.py`、`llmwiki floor-check` として起動）が掛かる：claim は明示的な affirmative token がある時のみ decision として記録され、沈黙は非承認として扱う。

FE-B' の抽出は **fork 対応**：単一ファイル読み取りではなく、セッション（`session_id`）を — その agent/fork 子（親の `session_id` を持つ）を含めて — vendored DuckDB views（`llmwiki/ingest/cc_views.sql`。`inspect-cc-log` skill の `views.sql` を byte 単位で vendor したコピーで、sync され contract test で drift ガードされる）から projector `llmwiki/ingest/cc_log_project.py` で投影する。注入された boilerplate を除去し、turn は content hash `md5(nfc_normalize(role) ‖ 0x1F ‖ nfc_normalize(text))` で **exact かつ長さ非依存**に dedup する（thinking ブロックは SQL レベルで除外）。wiki-local な **turn ledger**（`.cc-turn-ledger.jsonl`、`llmwiki/ingest/ledger.py` が書く）が各 owned turn の hash を初回 ingest で記録するため、同じセッションの再取り込み — またはセッション間で共有される prefix — でも各 turn は **1 回だけ** file される（first-ingested-owns、path 跨ぎ・再実行跨ぎで冪等）。ledger 差分はトランザクション内で journal され、さらに diff 自体が**トランザクションロック内**で走る：`begin` は turn 抽出をロック前（read-only）に行うが、seen-set の読み取りと owned turn の drop は `.llmwiki.lock` 保持後にのみ行うため、並行 ingest の `finish` が diff とロックの間に append で割り込めない（重複 file・first-owner 競合なし）。失敗したセッションは何も所有せず、次のセッションが共有 prefix を file し直す（欠落なし）。dedup/ledger の単位は **CC record**（`record_uuid`）であって会話 turn ではない：合成された replay record（1 record に複数の `USER:`/`ASSISTANT:` ブロックを埋め込んだもの）は 1 単位として扱い、会話粒度への分解は non-goal。

### セッション集合を ingest — `/wiki-ingest-sessions [--workspace | --pj <name>] [--root <wiki>]`

Path B は**解決されたセッション集合の全 cc-log セッション**を 1 コマンドで ingest する。セッション集合は次の優先順で解決する — 明示 `--workspace`（workspace 全体の `_projects/_state/*.json` を project filter なしで union）；なければ明示 `--pj <name>`（`_projects/_state/*.json` を `project == <name>` で filter）；なければ no-args で解決済み wiki scope（`resolve-root` の `WIKI_SCOPE`）に追従する：scope `workspace` は同じ workspace 全体 union、scope `pj`/`prompt` はこのセッション自身の taskflow-applied project（`_projects/_state/<sid>.json`）を解決し、未解決なら `--pj <name>` を促して fail-closed、scope `cwd`（不変・legacy/standalone）— および CWD 近傍の wiki である scope `child` — は実行中セッションの CC プロジェクトディレクトリ（ground-truth：現在のセッションの `<sid>.jsonl` を含むディレクトリ）を解決する。その後 session id をセッション開始タイムスタンプの昇順に並べ、既存の per-transaction ingest サイクルを **1 セッション 1 トランザクション**でループする（failure-continue、glob ループと同じ `N total / M succeeded / K failed / S dedup-skipped` サマリ）。dedup は ledger 駆動なので、プロジェクトの成長に伴う再実行は **incremental**：既に所有された turn は skip され、サマリは解決したセッション数と **ledger-skipped turn 数**も報告するため、incremental な再実行が無音の no-op になることはない。0 件マッチ / セッション集合解決不能は明示的エラー（fail-closed）。

`--pj <name>` スコープ（および no-args の `pj`/`prompt` 解決）は **taskflow が登録したセッションのみ**（`_projects/_state/*.json` の `project == <name>`）を対象にし、CC ディレクトリ全体ではない：`_state` ファイルの無いセッションは `--pj` 集合に入らない。`--workspace`（および workspace-scoped wiki での no-args）はこれを project 制限なしの全登録セッションに拡げる — あくまで taskflow が登録した範囲であり、CC ディレクトリ全体ではない。standalone/legacy リポジトリの全 CC セッションを対象にするには、cwd-scoped wiki で両フラグを省く（ドライバが CC ディレクトリを ground truth として解決する）。CC セッションログ corpus 全体（`~/.claude/projects`、および `$CLAUDE_CONFIG_DIR` 設定時はその `projects/` も）をセッションごとに再スキャンする（N セッション → N スキャン）のを避けるため、read-only の `project-batch` verb がループ前に全セッションの turn を **1 回**のスキャンで抽出する（per-session の turn ファイルを temp dir に書き、ループが cleanup する）。各 `begin` は `--turns` で抽出済み turn を受け取り、安価な per-session の dedup + ledger 差分のみを実行するので、ledger の read-after-write が逐次に保たれる。

### Query — `wiki-query` skill

wiki が active で**かつ off にトグルされていない**時に description 駆動で自動起動する（`wiki:off` はそのセッションの起動注入を抑止する）。`wiki/` と `wiki/derived/` の**両方**を読み、すべての claim をページパスで citation する。パスが trust tier を表す（`wiki/` = source、`wiki/derived/` = derived）。既定で read-only。明示的な自然言語の依頼（LLM 判断）がある時のみ回答を wiki へ filing する — 意図の判断を一切挟まず意図的に filing したい場合は `/wiki-file` を使う。どちらの経路でも redaction → write-tool ゲート → 単一トランザクションの包絡は同じ。

### 検索 backend（任意の外部依存）— qmd

既定の query 経路は `index`：`wiki-query` は全ページを列挙し（`llmwiki search <root>
--q …` が `scan-pages` と同じ集合を返す）、LLM が読むページを選ぶ — **外部依存なし・
従来と byte 等価**。大規模 wiki 向けに、外部のオンデバイス全文検索エンジン **qmd
（Quick Markdown Search、別途インストール・~GB のモデル）** に opt-in できる。wiki の
`SCHEMA.md` config で `search_backend: qmd` を設定すると、`search` verb が内部で qmd に
dispatch し、全列挙の代わりに関連度上位 k の ranked なページを返す。

- **opt-in かつ隔離。** qmd は同梱せず Python 依存も増やさない（`read/` 層が `qmd` CLI に
  shell-out する）。qmd の状態は全て `<wiki-root>/.qmd/` 配下（project-local、`wiki/`
  subtree のみ・`raw/` は index しない）。2 つの code ゲート（write allowlist・ingest
  transaction）は不変で、qmd はページを読むだけ。
- **正しさの境界。** qmd の各ヒットは `scan_pages`（page-ness の単一権威）を通す post-filter に
  掛けるため、`raw/` や `wiki/README.md` は決して cite されず、tier は依然パスで決まる（D22）
  — qmd が決めることはない。
- **`/wiki-reindex` で構築。** 一度（および大きな ingest の後に）実行して project-local index を
  構築/更新する（`qmd init` → `collection add wiki/` → `embed` → `update`）。書込は `.qmd/`
  配下のみ、冪等で、**`search_backend` が `qmd` でない、または qmd 未インストール時は no-op**
  （告知して exit、crash しない）。query 時に qmd が使えない場合、`search` は一行 loud-announce
  して index 経路へ degrade する — 同じ一行 degrade は query 途中の qmd エラーや
  空の結果（例：index が未構築）もカバーする。

### Lint — `/wiki-lint`

read-only。決定的な link / index グラフ検査（`llmwiki/lint/link_lint.py`、`llmwiki/core/wiki_index.py`、`llmwiki lint` として起動）に加え transcript 限定の型別 lint（v1）を実行し、優先順位付きの「next questions」リストを報告する。書込は一切しない。

transcript decision floor の affirmative-token 判定（`AFFIRMATIVE_TOKENS`、`transcript_floor.py`）は**英語専用**。日本語の transcript では、現状すべての `decisions` 候補がこのチェックに失敗し `FLOOR-VIOLATION` を発火する — floor は日本語コンテンツに対して事実上 inert であり、代わりに手動判断が必要になる。これは既知・受容済みの制限（日本語の肯定/否定は主に postfix であり、同じ token 方式では確実に検出できない）であり、不具合ではない。fail する方向は保守的（over-flag であり、false-admit ではない）に保たれる。

### Promote — `/wiki-promote <wiki/derived/X.md>`

derived な synthesis ページを source tier へ昇格する（`wiki/derived/X.md → wiki/X.md`）。コード駆動の move ＋ inbound link-rewrite（`llmwiki/write/promote.py`）で、明示的な人間承認と contamination チェックでゲートされる。フローは read-only verb と write verb に分割される：`llmwiki declare`（Step1 解決値宣言）、read-only `llmwiki promote-check`（Step2 承認**前**の contamination preview、move しない）、`llmwiki promote`（Step3 move、承認**後**のみ）。derived から source tier への唯一の経路。

### View — `/wiki-view`

active な wiki のローカル HTML ビューアを起動する — `127.0.0.1` bind の HTTP サーバ（ポート `17330`、`llmwiki-view view --serve`、内部は `llmwiki/view/generate_wiki_view.py`、外部公開しない）が wiki の Markdown ページをオンデマンドで HTML にレンダリングし、`[[wikilinks]]` をページ間の navigable なリンクにする。read-only（wiki には書き込まない）。wiki-root は多スコープ resolver（`--root` > pj > workspace > CWD）で解決するため、CWD が wiki root である必要はなくなった。明示する場合は `--root <path>` を渡す。

- 表示対象は `wiki/` + `wiki/derived/` のみ。`raw/` は**公開しない**。
- 各ページに tier バッジ（**source** / **derived**）を表示し、ページは **tier-distinct**：同名の `wiki/X.md` と `wiki/derived/X.md` は別ページ。basename が両方に解決する `[[X]]` は**両方**を tier 明示のリンク（`X (source)` / `X (derived)`）として描画する。
- 対象ページが存在しない `[[link]]` は、区別される navigable でない「missing」リンクとして表示する。
- **untrusted なページ内容への防御**: ページ本文は untrusted なソースから ingest されるため、レンダリング後の HTML を `nh3` でサニタイズし（script / イベントハンドラ / `javascript:` URL を除去、wikilink markup は温存）、第二層として全応答に厳格な `Content-Security-Policy`（`default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:`）を付与する。`Host` ヘッダが loopback 名（`127.0.0.1` / `localhost` / `::1`）でないリクエストは 403 で拒否する（DNS rebinding 対策）。
- **排他的ポート bind**: ポートが使用中の場合は起動を拒否する（`allow_reuse_address` 無効）— 別 wiki を配信する stale な viewer に静かに相乗りせず、bind エラーで「旧 viewer を停止するか `--port <other>` を指定せよ」と明示する（相乗りするとブラウザ接続の一部を stale 側が応答し、誤った wiki が表示される）。
- 起動時に URL ＋ ページ数を出力する（`[wiki-view] serving <N> pages at http://127.0.0.1:17330/ ...`）。停止は専用の **`/wiki-view-stop`** スキルで行い、ポート 17330 の listener をクロスプラットフォームに kill する：POSIX では `pkill -f "llmwiki-view view --serve"`、Windows/Git Bash では MSYS の `pkill` が native な `uv`/`python` プロセスを終了できないため `netstat -ano | grep ":17330 " | grep LISTENING | tr -d "\r" | sed "s/.* //" | sort -u | xargs -r -I{} taskkill //F //PID {}` でポート起点に kill する。非既定ポートで起動した場合は `/wiki-view-stop` に `--port <n>` を渡す。

## 設定とデフォルト（D3–D5）

config は `SCHEMA.md` frontmatter（wiki-local）にあり、Claude Code の設定機構ではなくプラグイン自身のスクリプトが読む。各軸は独立に解決され、優先順は **prompt-explicit > wiki-local config > built-in default**（D4）。あらゆる書込の前に、解決値とその出所を一行で宣言する（D5）。built-in default：

| キー | デフォルト | 意味 |
|---|---|---|
| `activation_scope` | `scoped` | `.llmwiki` wiki root の中でのみ起動（D3） |
| `read_grounding` | `implicit` | query は明示指示なしで wiki に接地（D3） |
| `write_mode` | `explicit` | 書込適用前に確認（D3）。`implicit` は確認を省略し、セッション冒頭で loud に告知 |
| `write_autocommit` | `auto` | INERT — エンジンは git を一切呼ばない。config 安定性のため保持 |
| `override_scope` | `operation` | prompt override は 1 操作に適用。`session` で sticky |
| `apply_fanout_k` | `10` | touch ページ ≤K はインライン、>K で per-cluster fan-out（D23） |
| `max_count` | `100` | 書込数 budget：per-apply-worker のページ上限、かつ ingest 粒度のゲート — touch 総数がこれを超えると human gate へエスカレート（F2） |
| `max_bytes` | `10485760` | write session あたりの書込サイズ budget（10 MiB）。超過は human gate へエスカレート |
| `search_backend` | `index` | query 読取経路：`index`（既定・外部依存なし）または `qmd`（opt-in の外部全文検索 backend） |
| `qmd_bin` | `qmd` | PATH で解決する qmd バイナリ（`search_backend=qmd` の時のみ使用） |
| `qmd_page_threshold` | `100` | wiki のページ数がこれを超える時のみ qmd を使う（以下は index 直） |

ingest のジャーナル checkpoint は毎回取得する（D14）：`write_mode` は「書込適用前に確認を出すか」のみを制御する。エンジンは git に commit することはない。

`max_bytes` は書込ごとではなく**累積**で強制される：fan-out した ingest（`apply_fanout_k`）では、同一トランザクション内の全 cluster を跨いで byte 累計が引き継がれるため、各 cluster が個別には上限内でも合算で budget を超える fan-out は拒否される（`REJECTED budget`、トランザクション全体がロールバック）。同じ budget は直接のページ書込（`file` verb。例：query 回答の filing）にも掛かり、ハードコードの default ではなく wiki の設定済み `max_count`/`max_bytes` を使う。

## ファイル構成

```
plugins/llm-wiki/
  .claude-plugin/plugin.json   # manifest: name / description / version / author.name
  hooks/
    hooks.json                 # UserPromptSubmit -> wiki_marker_inject.py
    wiki_marker_inject.py      # active wiki を解決 -> "wiki-active" ＋ "active wiki:" 行を注入。dormant 時は silent
  agents/
    wiki-ingest-extract.md     # Stage1 extract（tools: Read。書込ツールなし）
    wiki-ingest-apply.md       # Stage2 apply（page manifest を執筆・書込ツールなし）
    wiki-lint.md               # read 中心の lint subagent
  skills/                      # ユーザー向けエントリは全て skill（bare /wiki-*。plugin 名前空間 prefix なし）
    wiki-ingest-docs/SKILL.md     # /wiki-ingest-docs （ドキュメント ingest。単一ファイル / glob / ディレクトリ）
    wiki-file/SKILL.md            # /wiki-file （実行中セッションを filing。source/kind/cutoff 固定）
    wiki-ingest-sessions/SKILL.md # /wiki-ingest-sessions （Path B：解決されたセッション集合の cc-log セッションを ingest）
    wiki-init/SKILL.md            # /wiki-init（対話的に scope 選択 -> llmwiki init）
    wiki-lint/SKILL.md            # /wiki-lint（read-only lint ディスパッチ）
    wiki-promote/SKILL.md         # /wiki-promote（derived -> source）
    wiki-query/SKILL.md           # query skill（description 駆動の自動起動）
    wiki-reindex/SKILL.md         # /wiki-reindex（任意の qmd 検索 index を再構築。.qmd/ のみ）
    wiki-view/SKILL.md            # /wiki-view（ローカル HTML ページビューアを起動）
    wiki-view-stop/SKILL.md       # /wiki-view-stop（ビューア停止。ポート 17330 をクロスプラットフォームに解放）
  llmwiki/                     # path-import されるパッケージ（install 不要）。決定的エンジン
    __init__.py                # version ＋ 公開 re-export
    cli.py                     # verb dispatch（branch-local lazy import で read-only profile を強制）
    core/                      # 単一権威・dep-free
      wiki_index.py marker.py config_resolver.py wiki_root_resolver.py wiki_log.py content_hash.py wiki_toggle.py   # wiki_toggle: per-session wiki:on|off 状態
    write/                     # allowlist write ゲート ＋ promote
      write_tool.py transaction.py promote.py
    ingest/                    # duckdb
      ingest_driver.py frontends.py redaction.py transcript_floor.py
      cc_log_project.py ledger.py cc_views.sql   # fork 対応 cc-log projector ＋ turn-hash dedup ledger ＋ vendored SQL views
    read/                      # dep-free な読取経路（qmd は外部 CLI の shell-out で、Python 依存ではない）
      query.py qmd_search.py
    lint/   link_lint.py       # graph/index lint
    view/   generate_wiki_view.py  # ローカル HTML ビューア（markdown）
    init/   wiki_init.py       # wiki 初期化
  bin/                         # CLI entrypoint（PEP 723 で依存宣言・uv run）
    llmwiki                    # dep-free: resolve-root scan-pages search file declare promote-check promote lint init marker-detect ingest-apply floor-check reindex
    llmwiki-ingest             # duckdb:   ingest {begin|plan-fanout|finish|apply-finish|abort|enumerate|session-plan|project-batch|project-batch-cleanup}
    llmwiki-view               # markdown: view --serve
  pyproject.toml               # version / requires-python / extras(doc)。runtime は install しない
  templates/                   # 新しい wiki インスタンスの初期化元
    .llmwiki SCHEMA.md index.md log.md raw/ wiki/
```

## ライセンス

[MIT](../../LICENSE)
