# llm-wiki

**LLM が維持管理する wiki** を実現する Claude Code プラグイン。ソースを Markdown ページに ingest し、それらのページに接地して質問に答え、wiki グラフを lint する。プラグインは**不変エンジン**（D1）であり、ingest / query / lint の手続きと、per-wiki repo の初期化元となる wiki 契約の**スキーマテンプレート**を同梱する。wiki の契約自体は書き換えない。各 wiki は独立した per-wiki repo で、自身の schema / index / log / raw / pages を保持し、それらが co-evolve する。

[English README is here](README.md)

## これが解決する問題

メモや決定はチャット・コマンド出力・セッションログに散在する。llm-wiki はそれらを不変の `raw/` アーティファクトに正規化し、コードで強制される安全境界の下で LLM に wiki ページの執筆・更新を行わせる：untrusted なソース読取とページ書込を分離し、すべての書込を allowlist ゲートに通し、ingest 全体を単一の git トランザクションとして走らせる（失敗時は wiki を ingest 前の状態にロールバック）。

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

プラグインの決定的スクリプトと hook は `uv run python` 経由で動く。プラグインのロードに別途 `init` ステップは不要。`UserPromptSubmit` hook はプラグインを有効化した時点で起動する。

## wiki の初期化

wiki は **`/wiki-init`** で初期化する。選択した scope に、wiki を**独立した nested git repo**（自前の `git init` ＋ 初期 commit）として作成する。scope は対話的に選ぶ：

- **taskflow 有効かつ project 割当あり** — **active pj**（`_projects/<project>/wiki/`）／ **workspace**（`<workspace-root>/_llm-wiki/`）／ **path 入力** から選ぶ。
- **taskflow 無効または project 未割当** — **project を選ぶ**（`$TASKFLOW_PROJECT_ROOTS`、無ければ `_projects/` を走査）／ **workspace** ／ **path 入力**。
- active 以外の project を対象にする場合は `--root <path>` を渡す（その project は選択肢には追加しない）。

```
/wiki-init                       # 対話的に scope を選択
/wiki-init --root ./path/to/wiki # 対象 root を明示し選択を省略
```

wiki は自身が独立 repo なので、ingest ロールバックの `git reset --hard` が親 repo に届くことはない。`/wiki-init` は周囲の親 repo を検出し、wiki-root の相対パスを**親 repo の `.git/info/exclude`** に登録する — repo-local で commit されないため、親は wiki を追跡しない。注意：後で wiki を削除しても `.git/info/exclude` の行は残る。手動で削除すること。

wiki はプラグインの `templates/` から初期化される：

- `.llmwiki` — wiki **marker**（D8）：`{ version, schema: SCHEMA.md }`。その存在がディレクトリを wiki root として標識する。検出専用で config は持たない。
- `SCHEMA.md` — wiki **契約**：規約 prose ＋ `config` と `doc_type_profiles` を持つ YAML frontmatter（8 つの doc type を全て seed、加えて必須の `default`）。
- `index.md` — content-oriented なカタログ seed。
- `log.md` — append-only ログ seed。grep 解析可能な `## [YYYY-MM-DD] <op>|<provenance-or-origin> | <Title>` prefix 規約。
- `raw/` — 不変・redaction 済みのソースアーティファクト（id は content-hash。LLM は読むのみ）。
- `wiki/` — LLM が執筆するページ。`wiki/` は source tier、`wiki/derived/` は未昇格の synthesis。

## active な wiki の解決

操作は active な wiki を**存在ベース**で上から解決する：**prompt `--root` > pj（`_projects/<project>/wiki/`）> workspace（`_llm-wiki/`）> CWD `.llmwiki`**。pj scope は taskflow から一方向に読む（最新の `_projects/_state/*.json` の `project` field を `$TASKFLOW_PROJECT_ROOTS` で解決）。state file が無ければ pj scope は綺麗にスキップする。解決が CWD だけに依存しなくなったため、`cd` の無い **VSCode 拡張でも動作する**。marker hook は毎ターン `active wiki: <root> (scope: pj|workspace|cwd)` を表示し、解決された wiki が常に可視になる。

## 使い方

典型的なセッション：

1. **wiki に入る。** 解決は自動（上記「active な wiki の解決」参照）— wiki root へ `cd` するか、pj/workspace scope に任せるか、`--root <path>` を渡す。scope hook はそのターンに `wiki-active` と `active wiki:` 行を注入する。何も解決しなければプラグインは不可視のまま。

2. **ソースを ingest する。**

   ```
   /wiki-ingest ./docs/rfc-routing.md            # 3rd-party ドキュメント（FE-B → source tier）
   /wiki-ingest ./docs/rfc.md external=https://example.com/rfc   # citation 用の permalink を付ける
   /wiki-ingest ./logs/session.jsonl             # Claude Code セッションログ（FE-B' → derived tier、doc_type=transcript に pin）
   ```

   引数は**クォートした glob** や**ディレクトリ**も指定できる — driver が（シェルではなく）Python で展開し、**1 ファイル 1 トランザクション**で ingest する：

   ```
   /wiki-ingest "./docs/**/*.md"                 # クォートした glob：Python で展開、wiki 内部パスを除外、1 ファイル 1 トランザクション
   /wiki-ingest ./docs/                          # ディレクトリ：./docs/**/* として展開し、テキスト系 allowlist に限定
   ```

   ディレクトリの場合はテキスト系 allowlist（`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`）のみ拾い、非テキスト（例：画像）はスキップする。バッチでは 1 ファイルの失敗は**そのファイルだけ**ロールバックして続行し、末尾に `N total / M succeeded / K failed / S dedup-skipped` のサマリを報告する。0 件マッチはエラー。

   何かが書き込まれる前に、解決値の一行宣言（`[wiki] write_mode = explicit (default)` …）が出る。既定の `write_mode=explicit` では Stage 2 のページ適用前に確認する。ingest 全体は（ファイル単位の）単一 git トランザクションなので、失敗や却下時は wiki が ingest 前の状態にロールバックする。

3. **質問する。** 自然言語で尋ねるだけ — 例：*「retry backoff について何を決めたっけ？」*。`wiki-query` skill が自動起動し、`wiki/` と `wiki/derived/` の両方を読み、各 claim をページパスで citation する（パスが source / derived tier を示す）。これは read-only。

4. **回答を filing（任意・明示）。** query 自体は書き込まない。回答を保存したい時は明示的に依頼する — 例：*「それをページとして filing して」* — と `wiki/derived/` 配下に derived synthesis として着地する。

   LLM の意図判断に依存しない**決定的**な filing トリガとして、通常の質問の任意の位置に marker `llm-wiki:file` を含められる — hook が検出し filing を必須化する（確認なし：marker は定義上明示的）。回答は `wiki/derived/` 配下に filing される：

   ```
   retry backoff について何を決めたっけ？ llm-wiki:file              # filing を強制。ページ名は回答から生成
   retry backoff について何を決めたっけ？ llm-wiki:file=retry-policy # ページ名を固定 → wiki/derived/retry-policy.md
   ```

   `llm-wiki:file=<page-slug>` はページ名を `wiki/derived/<page-slug>.md` に固定する。slug 無しの時は LLM が回答からページ名を生成する。marker は wiki の中（`.llmwiki` がある時）でのみ有効。安全境界（redaction → write-tool の location ゲート → 単一トランザクション）は不変で、省略されるのは確認プロンプトのみ。

5. **Lint。** `/wiki-lint` がグラフ / index 検査と transcript の decision floor を実行し、優先順位付きの「next questions」リストを返す。read-only。

6. **wiki を閲覧する。** `/wiki-view` がローカル HTML ビューア（`http://127.0.0.1:17330/`）を起動し、wiki の `wiki/` + `wiki/derived/` ページをレンダリングして `[[wikilinks]]` をクリックで辿れるようにする。read-only。停止は `pkill -f "generate_wiki_view.py --serve"`。

7. **Promote。** `wiki/derived/` のページが source tier に値する時：`/wiki-promote wiki/derived/retry-policy.md`。明示的な承認と contamination チェックの後、`wiki/retry-policy.md` へ move し inbound link を rewrite する。derived→source の唯一の経路。

**回復（recovery）。** ingest が中断（プロセスが途中で kill）されると `.llmwiki.lock` が残ることがある。手動でクリアする — ingest 前の checkpoint へロールバックし lock を解放する：

```bash
uv run python "${CLAUDE_PLUGIN_ROOT}/scripts/ingest_driver.py" abort <wiki-root>
```

## 操作

すべての操作は wiki root（`.llmwiki` を持つディレクトリ）から実行する。

### scope 検出

`activation_scope: scoped` は `UserPromptSubmit` hook（`hooks/wiki_marker_inject.py`）として実装される：毎ターン CWD の `.llmwiki` marker を検出し、存在すれば `wiki-active` コンテキストを注入する。marker が無ければ silent に exit し何も注入しない（wiki の外ではプラグインは不可視）。注入されるコンテキストと `wiki-query` skill の description が query を自動起動させる。書込を伴う操作は明示的コマンドで、hook には依存しない。

### Ingest — `/wiki-ingest <path-or-source-or-glob> [doc_type=...] [external=...]`

3rd-party ソース（FE-B）または Claude Code セッション jsonl（FE-B'）を、2 段 `extract → apply` core を通して単一 git トランザクション内で ingest する。引数は単一ファイルのほか、**クォートした glob**（`"./docs/**/*.md"`）や**ディレクトリ**（`./docs/`）も指定できる：driver が（シェルではなく）Python で展開し、wiki 内部パスを強制除外し、ディレクトリの場合はテキスト系 allowlist（`.md` / `.markdown` / `.txt` / `.text` / `.json` / `.jsonl`）に限定する。glob/ディレクトリは**1 ファイル 1 トランザクション**で ingest し、1 ファイルの失敗はそのファイルだけロールバックして続行、末尾に `N total / M succeeded / K failed / S dedup-skipped` のサマリを報告する（0 件マッチはエラー）。

- **Stage 1（extract）** — `wiki-ingest-extract` subagent が redaction 済み・untrusted の raw ソースを**構造的に書込ツールなし**で読み、提案編集のみを出力する。
- **Stage 2（apply）** — `wiki-ingest-apply` subagent がページ更新を執筆し、すべての書込を allowlist write ツール（`scripts/write_tool.py`）経由でのみ stage する。同ツールは書込先を `wiki/`・`wiki/derived/` に限定し、`SCHEMA.md` / `.llmwiki` / `raw/` / 絶対パス / traversal を拒否し、budget でゲートする。touch ページが `apply_fanout_k` を超えると Stage 2 は per-cluster の apply worker に fan-out する。index / log / commit は join 後に中央集約される。

cc-log（FE-B'）入力は `doc_type=transcript` に pin され、決定的な decision floor（`scripts/transcript_floor.py`）が掛かる：claim は明示的な affirmative token がある時のみ decision として記録され、沈黙は非承認として扱う。

### Query — `wiki-query` skill

wiki が active な時に description 駆動で自動起動する。`wiki/` と `wiki/derived/` の**両方**を読み、すべての claim をページパスで citation する。パスが trust tier を表す（`wiki/` = source、`wiki/derived/` = derived）。既定で read-only。明示的な filing トリガがある時のみ回答を wiki へ filing する — 自然言語の依頼（LLM 判断）か、質問の任意の位置に含めた決定的・hook 検出の marker `llm-wiki:file[=<page-slug>]` のいずれか。marker は確認なしで filing を強制する。slug 指定時はページ名を `wiki/derived/<page-slug>.md` に固定し、無指定時は LLM が生成する。省略されるのは確認のみで、redaction → write-tool ゲート → 単一トランザクションの包絡は不変。

### Lint — `/wiki-lint`

read-only。決定的な link / index グラフ検査（`scripts/link_lint.py`、`scripts/wiki_index.py`）に加え transcript 限定の型別 lint（v1）を実行し、優先順位付きの「next questions」リストを報告する。書込は一切しない。

### Promote — `/wiki-promote <wiki/derived/X.md>`

derived な synthesis ページを source tier へ昇格する（`wiki/derived/X.md → wiki/X.md`）。コード駆動の move ＋ inbound link-rewrite（`scripts/promote.py`）で、明示的な人間承認と contamination チェックでゲートされる。derived から source tier への唯一の経路。

### View — `/wiki-view`

active な wiki のローカル HTML ビューアを起動する — `127.0.0.1` bind の HTTP サーバ（ポート `17330`、`scripts/generate_wiki_view.py --serve`、外部公開しない）が wiki の Markdown ページをオンデマンドで HTML にレンダリングし、`[[wikilinks]]` をページ間の navigable なリンクにする。read-only（wiki には書き込まない）。wiki-root は多スコープ resolver（`--root` > pj > workspace > CWD）で解決するため、CWD が wiki root である必要はなくなった。明示する場合は `--root <path>` を渡す。

- 表示対象は `wiki/` + `wiki/derived/` のみ。`raw/` は**公開しない**。
- 各ページに tier バッジ（**source** / **derived**）を表示し、ページは **tier-distinct**：同名の `wiki/X.md` と `wiki/derived/X.md` は別ページ。basename が両方に解決する `[[X]]` は**両方**を tier 明示のリンク（`X (source)` / `X (derived)`）として描画する。
- 対象ページが存在しない `[[link]]` は、区別される navigable でない「missing」リンクとして表示する。
- 起動時に URL ＋ ページ数を出力する（`[wiki-view] serving <N> pages at http://127.0.0.1:17330/ ...`）。停止は `pkill -f "generate_wiki_view.py --serve"`。

## 設定とデフォルト（D3–D5）

config は `SCHEMA.md` frontmatter（wiki-local）にあり、Claude Code の設定機構ではなくプラグイン自身のスクリプトが読む。各軸は独立に解決され、優先順は **prompt-explicit > wiki-local config > built-in default**（D4）。あらゆる書込の前に、解決値とその出所を一行で宣言する（D5）。built-in default：

| キー | デフォルト | 意味 |
|---|---|---|
| `activation_scope` | `scoped` | `.llmwiki` wiki root の中でのみ起動（D3） |
| `read_grounding` | `implicit` | query は明示指示なしで wiki に接地（D3） |
| `write_mode` | `explicit` | 書込適用前に確認（D3）。`implicit` は確認を省略し、セッション冒頭で loud に告知 |
| `write_autocommit` | `auto` | `write_mode=implicit` の時 `true` 強制（floor、D5） |
| `override_scope` | `operation` | prompt override は 1 操作に適用。`session` で sticky |
| `apply_fanout_k` | `10` | touch ページ ≤K はインライン、>K で per-cluster fan-out（D23） |

ingest の git checkpoint は `write_mode` に関わらず毎回取得する（D14）：`write_mode` は「書込適用前に確認を出すか」のみを制御し、wiki を commit するか否かは制御しない。

## ファイル構成

```
plugins/llm-wiki/
  .claude-plugin/plugin.json   # manifest: name / description / version / author.name
  hooks/
    hooks.json                 # UserPromptSubmit -> wiki_marker_inject.py
    wiki_marker_inject.py      # active wiki を解決 -> "wiki-active" ＋ "active wiki:" 行を注入。dormant 時は silent
  commands/
    wiki-ingest.md             # /wiki-ingest  （ingest オーケストレータ）
    wiki-lint.md               # /wiki-lint    （read-only lint ディスパッチ）
    wiki-promote.md            # /wiki-promote （derived -> source）
  agents/
    wiki-ingest-extract.md     # Stage1 extract（tools: Read。書込ツールなし）
    wiki-ingest-apply.md       # Stage2 apply（書込は allowlist ツール経由のみ）
    wiki-lint.md               # read 中心の lint subagent
  skills/
    wiki-init/SKILL.md         # /wiki-init（対話的に scope 選択 -> wiki_init.py）
    wiki-query/SKILL.md        # query skill（description 駆動の自動起動）
    wiki-view/SKILL.md         # /wiki-view（ローカル HTML ページビューアを起動）
  scripts/                     # 決定的エンジン（uv 実行可能）
    config_resolver.py marker.py redaction.py content_hash.py frontends.py
    extract_cc_log.py wiki_log.py wiki_index.py link_lint.py write_tool.py
    transaction.py promote.py transcript_floor.py generate_wiki_view.py
    wiki_root_resolver.py wiki_init.py
  templates/                   # 新しい wiki インスタンスの初期化元
    .llmwiki SCHEMA.md index.md log.md raw/ wiki/
```

## ライセンス

[MIT](../../LICENSE)
