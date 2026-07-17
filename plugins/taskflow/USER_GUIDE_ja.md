# taskflow — ユーザーガイド

日常利用のための、タスク指向のやさしいガイド。内部設計は [`docs/architecture.md`](docs/architecture.md)、機能リファレンスは [README](README_ja.md) を参照。このページは taskflow の *使い方* を示す。

[English version](USER_GUIDE.md)

---

## 1. taskflow は何をしてくれるか

Claude Code で多数のセッション・複数の並行作業をこなすと、2 つのものが失われがちだ。

- **今どのプロジェクトで、何をしていたか** — 新しいセッションは毎回まっさらから始まる。
- **あの決定・調査・やりかけのタスクはどこへ？** — 探しづらい過去のチャットの中にしか残っていない。

taskflow はこれを、プロジェクトごとに小さな Markdown ファイル群を保持し、毎ターンの冒頭で適切なものを自動的に Claude に戻すことで解決する。あなたは普通の言葉で作業を続け、記憶は taskflow が保つ。

### メンタルモデル: プロジェクトごとに 3 つのストア

```mermaid
flowchart LR
    subgraph P["_projects/&lt;project&gt;/"]
        PR["progress.md<br/>タスク一覧<br/>（俯瞰用）"]
        T["tasks/<br/>1 タスク 1 ファイル<br/>（実作業）"]
        N["project-notes/<br/>永続知識<br/>（仕様・調査…）"]
    end
    T -- "自動 rebuild で反映" --> PR
    T -. "link" .-> N
```

| ストア | 例えるなら | あなたが編集するのは… |
|---|---|---|
| **`progress.md`** | ダッシュボード — 全タスクと状態の表 | ほぼ不要（表は自動生成） |
| **`tasks/`** | 付箋 — タスクごと 1 枚、状態フォルダに整理 | `/progress` コマンドと通常作業を通じて |
| **`project-notes/`** | プロジェクトの書庫 — 永続的な知識 | 「notes に保存して」と頼んで |

基本はいつも通り Claude と話すだけ。整理は taskflow がやる。

---

## 2. はじめる（設定は不要）

1. プラグインを導入:

   ```
   /plugin marketplace add gigamori/ai-agent-toolkit
   /plugin install taskflow@ai-agent-toolkit
   ```

2. workspace で **初めて `pj:<project>` を使用する**と、taskflow が `_projects/` フォルダと空のプロジェクト index を自動生成する — セットアップ手順は不要。

3. プロジェクトを名指しし始める（次節）。

> **Claude Code 専用。** taskflow は毎ターンのコンテキスト注入に依存しており、Cursor の同等フックはそれを行えない。Cursor 上では動作しない。

---

## 3. `pj:` でプロジェクトを選ぶ

各セッションは最大 1 つのプロジェクトに属する。プロンプトの冒頭（最初のほう）に `pj:<name>` を置いて宣言する。

```mermaid
flowchart TD
    A["プロンプトを送る"] --> B{"pj:… で始まる？"}
    B -- "pj:my-app" --> C["'my-app' に切替 / 維持"]
    B -- "（省略）" --> D["このセッションで直前に設定した<br/>プロジェクトを維持"]
    B -- "pj:none" --> E["該当プロジェクトなし、と宣言"]
    B -- "pj:?" --> F["関連度順にプロジェクトを列挙<br/>（何も変更しない）"]
    B -- "norouter" --> G["このターンは taskflow を完全無視"]
    C --> H["taskflow がプロジェクトの context を注入:<br/>index + progress + 作業中タスク"]
    D --> H
    H --> I["Claude がその context で作業"]
```

| 入力 | 起きること |
|---|---|
| `pj:my-app ログインのバグを直して` | プロジェクト `my-app` で作業 |
| `次のやつ直して`（`pj:` なし） | 直前に設定したプロジェクトを維持 |
| `pj:?` または `pj:? 課金パイプライン` | 近いプロジェクトを列挙して選ばせる |
| `pj:none ちょっとしたスクリプト書いて` | これはプロジェクト作業ではないと宣言 |
| `norouter これだけ答えて` | 単発 — taskflow は一切関与しない |

重要な原則: **taskflow はプロジェクトを推測しない。** 名指しもなく設定もなければ、静かに何もしない。

### 新規プロジェクトの作成

こう頼むだけ: *「billing-revamp という新しいプロジェクトを作って」*。Claude が確認したうえで、そのプロジェクトの `index.md` / `progress.md` / `project-notes/index.md` を scaffold し、マスター index に追加する。作成前に必ずあなたの承認を取る。

---

## 4. 日常のループ

普通の作業セッションを端から端まで見ると、こうなる。

```mermaid
sequenceDiagram
    participant You as あなた
    participant Claude
    participant TF as taskflow (hooks)
    participant Files as _projects/ files

    You->>Claude: 「pj:my-app パスワード再設定フローを追加して」
    TF->>Files: project index + progress + 作業中タスクを読む
    TF-->>Claude: その context を注入
    Claude->>Files: コードを書き、タスクファイルを更新
    Note over TF,Files: 各書込後 — progress テーブルを rebuild、<br/>触ったファイルを静かに記録
    Claude-->>You: 「完了 — 再設定フローです」
    Note over TF: セッション終了
    TF->>Files: このセッションの作業を owning task に log
```

真ん中の手順をあなたが実行することはない。指示を出せば Claude が作業し、`progress.md` とタスクの log は taskflow が自動で最新に保つ。

---

## 5. `/progress` でタスクを管理する

`/progress` は、タスクの参照と移動をまかなう 1 つのコマンド。後ろは自然言語でよい — Claude が action と対象を判定し、plan を見せ、破壊的変更の前には必ず確認する。

### タスクのライフサイクル

タスクは単なるファイルで、**フォルダが状態そのもの**:

```mermaid
stateDiagram-v2
    [*] --> todo: タスク作成
    todo --> in_progress: /progress start
    in_progress --> done: /progress approve （あなたが承認）
    in_progress --> todo: /progress revert
    done --> in_progress: /progress revert （再開）
    done --> [*]
```

| やりたいこと | こう言う |
|---|---|
| 要注意点を見る（drift・stale・承認待ち） | `/progress check` |
| 残作業で全タスクを分類 | `/progress audit` |
| タスクに着手 | `/progress start migration` · `/progress 着手 migration` |
| 完了にする（要あなたの OK） | `/progress approve migration` · `/progress 完了 migration` |
| 差し戻す / 再開 | `/progress revert migration` |
| ダッシュボードの表を更新 | `/progress rebuild` |

- タスクを **Done に入れるには必ずあなたの明示的な承認が要る** — Claude が勝手に完了扱いにすることはない。
- 確信があるときは `-y` を付けて確認を省ける（例: `/progress 全部完了 -y`）。
- 言い回しが何にも一致しない（または曖昧に複数一致する）場合、Claude は推測せず候補を列挙して止まる。

### タスクファイルの中身

手で編集することはほぼないが、ただの Markdown だ:

```markdown
---
priority: HIGH
created: 2026-05-13
updated: 2026-05-14
---

# パスワード再設定フローの追加

作業メモをここに書く（自由に書き換え可）。

## Next Steps
- メールテンプレートを配線
- テスト追加

<!-- @log:begin -->
- 2026-05-13 [s:abc12345]: 着手
- 2026-05-14 [s:def67890]: フォーム + endpoint 完了 | next: メールテンプレート
<!-- @log:end -->
```

- **`## Next Steps`** は正直な「残り」リスト。ガイドラインがエージェントにタスクを前進させたターンの終わりに書き直すよう指示し、`/progress audit` が検査する。
- **`@log` ブロックは履歴** — append-only、1 セッション 1 行。タスクがどう進んだか、そして作業した当のセッションへ、いつでも遡れる。

---

## 6. project-notes に知識を残す

タスクは一時的。永続させるべき発見もある。`project-notes/` は永続知識をカテゴリ別に整理する場所だ。

```mermaid
flowchart TD
    Q["あなた: 「これ Postgres と SQLite どっちがいい？比較して」"] --> R["Claude が回答"]
    R --> S{"調査 / 比較 / 整理だった？"}
    S -- はい --> T["Claude が保存を提案<br/>（カテゴリ + 名前の候補付き）"]
    T -- 承認 --> U["project-notes/&lt;category&gt;/… に保存<br/>notes index にも追加"]
    S -- "いいえ（質問・些末な修正）" --> V["何も保存しない"]
```

| カテゴリ | 用途 |
|---|---|
| `specs/` | 仕様・設計・決定・ADR |
| `investigations/` | 調査・分析・post-mortem |
| `checks/` | チェックリスト・確認項目 |
| `procedures/` | 人間向けの手順書 |
| `backlog/` | アイデア・候補 |
| `_archive/` | もう権威でない |

普通の言葉での使い方:

| こう言う | 結果 |
|---|---|
| 「この調査を notes に保存して」 | Claude が保存し notes index を更新 |
| 「notes に何がある？」 | 関連 notes を列挙（タイトルのみ） |
| 「このリポの構造を notes にまとめて」 | Claude が調査し、保存を提案 |

調査系の依頼では、Claude が *能動的に* 保存を提案する — 保存前に必ずあなたが承認し、「はい」のときだけ保存される。単なる質問・デバッグ・些末な編集では提案されない。

---

## 7. 全体を一望する — `/kanban`

`/kanban` を実行すると、全プロジェクト・全タスクの可視化ボードが得られる。

```mermaid
flowchart LR
    K["/kanban"] --> S["ローカルボード<br/>http://localhost:&lt;port&gt;/（ワークスペース毎）"]
    S --> V1["ステータス別<br/>TODO · In Progress · Done"]
    S --> V2["プロジェクト別<br/>1 プロジェクト 1 列"]
    V1 --> L["タスクをクリック → その<br/>セッションログ / /progress アクションへ"]
    V2 --> L
```

- 小さなローカルサーバーをバックグラウンド起動し、URL と停止コマンドを表示する。
- 2 つの切替ビュー（ステータス別・プロジェクト別）、優先度バッジ、プロジェクト/ステータスでのフィルタ。
- タスクのセッション履歴をクリックすると、その作業をした当のセッションが開く。

---

## 8. taskflow が自動でやること

以下はあなたが管理するものではない — ファイルが最新に保たれる *理由* を知ってもらうために載せる。taskflow は決まったタイミングで小さなスクリプトを走らせる。

```mermaid
flowchart TD
    subgraph turn["各ターン"]
        A["プロンプトが Claude に届く前:<br/>プロジェクトを設定/維持し、context・guidelines + プロジェクトルールを注入"]
        B["各ファイル書込の後:<br/>progress テーブルを rebuild、触ったファイルを記録"]
    end
    subgraph end_["セッション終了時"]
        C["新しい plan/memory ファイルをプロジェクトにコピー"]
        D["作業した各タスクに log 行を書き、<br/>Next Steps を更新"]
    end
    A --> B --> C --> D
```

知っておくと便利な 2 点:

- **自動ロギング。** タスク自身のファイルの *外* を編集した場合でも、taskflow はその作業を正しいタスクに帰属できる。手作業の記帳なしで履歴が完全に保たれる。
- **note ↔ task link。** セッションが永続 note を生むと、taskflow が owning task からその note への link を記録する。あとで名前が変わっても両者はつながったままになる。

このシステムは append-only 書込みと有界ロックで保護されており、`progress.md` の自動テーブルはタスクファイルからいつでも rebuild できる。既知の残余リスクは R-lock（ツール層の Edit との並行 append）で、stderr に記録される。

### 任意: プロジェクト固有ルール（`rules.md`）

プロジェクトには短い `rules.md` を持たせられる — Claude に守らせたいプロジェクト固有のルール（例：*「`src/` を編集し、`dist/` は直接編集しない」*）。taskflow プロジェクト単位のスコープなので `pj:` で切り替わる — リポジトリ全体の `CLAUDE.md` や、パス単位の `.claude/rules` とは異なる。

- **設定**: `/pj-rules show` で現状確認、`/pj-rules このプロジェクトに … というルールを追加して`（または普通に頼むだけでも可）で変更を diff として提示し、承認後にのみ適用する — このファイルは以降の全ターンに影響するため、確認をスキップする手段は無い。`_projects/<project>/rules.md` を手で編集してもよい。Claude がこのファイルを勝手に書き換えることはない。
- **Claude への届き方**: プロジェクトに切り替えた時に全文を 1 回提示し、以降のターンは `##` 見出しだけを「行動前に読み直せ」というリマインダとして再掲する。ファイルは短く保つ（デフォルト予算 ~100 行、現在の行数は `/pj-rules show` で確認可）。全文を毎ターン視界に残したい場合は frontmatter に `inject_every_turn: true` を設定する。

---

## 9. クイックリファレンス

| 目的 | やること |
|---|---|
| プロジェクトで作業 | プロンプトを `pj:<name>` で始める |
| 適切なプロジェクトを探す | `pj:?` |
| 単発、taskflow を無視 | `norouter` で始める |
| プロジェクト作成 | 「… という新しいプロジェクトを作って」と頼む |
| タスクの着手/完了/再開 | `/progress start\|approve\|revert <name>` |
| タスクの健全性チェック | `/progress check` · `/progress audit` |
| ダッシュボード更新 | `/progress rebuild` |
| 永続知識を保存 | 「これを notes に保存して」 |
| プロジェクト固有ルールの閲覧/設定 | `/pj-rules show` · `/pj-rules … というルールを追加して`（常に diff 提示・承認制） |
| 全体を見る | `/kanban` |

**黄金律**

1. taskflow はプロジェクトを *推測しない* — 名指しするか、静かにしているか。
2. タスクが **Done** になるのは、あなたの明示的な承認があるときだけ。
3. 自動管理領域（`progress.md` の `@table`、タスクの `@log` / `@notes`）は手編集しない。Claude に頼むか `/progress rebuild` を使う。
