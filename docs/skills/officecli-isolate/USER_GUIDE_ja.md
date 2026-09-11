# officecli-isolate — ユーザガイド

Word / Excel / PowerPoint ファイルを officecli 公式のドキュメントスキルで作成・編集する。ただし作業の中身は自分のセッションに現れない。このスキルはファイル 1 つにつき child session を 1 つ起動し、どの officecli スキルを load するかを指示し、入力を渡し、短い固定形式の報告だけを受け取る。スクリーンショット、レイアウトの試行錯誤、CLI の help 出力、batch payload は child の中に留まる。

**起動**: このスキルは `/officecli-isolate` と明示的に呼んだときだけ動く。エージェントが自発的に起動することはない。

## 渡すもの

| 入力 | いつ | 内容 |
|------|------|------|
| Format | 常に | `docx` / `xlsx` / `pptx` |
| Content | 新規作成・差し替え | `docx` / `pptx`: markdown 1 本。`xlsx`: シートごとに CSV 1 本 |
| Original | 部分更新・差し替え | 既存ファイル。直接は編集されず、child は出力 path のコピーに対して作業する |
| 指示書 | 部分更新（必須）、`xlsx` 新規作成（任意のレイアウト指示）、`xlsx` / `pptx` 差し替え（必須の範囲指定） | 小さな markdown。形式は後述 |
| 出力 path | 任意 | 既定は content または original と同じディレクトリ・同じ stem（更新時は `-out` 接尾辞） |
| Genre | 任意 | load する officecli スキル名。既定は format の base スキル |

## 3 つのケース

ケースは「どの入力があるか」で決まる。依頼の言い回しでは決めない。

| 渡したもの | ケース | child の作業 |
|-----------|--------|--------------|
| content のみ | **create** | 新規ファイルを作る。`docx` / `xlsx` は officecli 純正の markdown / CSV 取込による転記。`pptx` は load したデッキスキルの下で著述し、媒体向けの書き換えを行ってよい |
| original + 指示書 | **update-partial** | 列挙された編集だけをコピーに適用する。各 location を stable-id path に解決し、その id を返す |
| original + content（`xlsx` / `pptx` は範囲指定も） | **replace** | コピー上で本文（`docx`）、対応シートのデータ領域（`xlsx`）、slide 範囲（`pptx`）を置き換える。styles、header / footer、数式、chart、theme、master は残す |

表にない組み合わせを渡すと、推測せずにどのケースかを尋ねる。

## Genre

officecli は format ごとに base スキル 1 本と、その上に載る scene layer をいくつか持つ。child は必ず 1 本だけ load し、content から推測することはない。呼び出し側が名前を指定するか、base が使われる。

| Format | 既定 | 指定できるもの |
|--------|------|----------------|
| docx | `word` | `academic-paper`, `word-form` |
| pptx | `pptx` | `pitch-deck`, `morph-ppt`, `morph-ppt-3d` |
| xlsx | `excel` | `financial-model`, `data-dashboard` |

## 指示書

完全な形式はスキル内の `references/instruction-formats.md` にある。

**部分更新** — 編集 1 件につき 1 ブロック:

```markdown
## 1
location: find:DRAFT
value: FINAL
tracked: Alice
```

`location` は officecli path（`/body/p[3]`）、stable-id path（`/slide[2]/shape[@id=550950021]`）、または一意に一致しなければならない `find:<text>` のいずれか。`value` はテキスト、`prop: key=value` はプロパティを設定する。`tracked`（docx）は変更履歴付きにする。

**xlsx レイアウト**（新規作成時、任意）: `sheets`、`columns`、`freeze`、`autofilter`、`charts`、`print` の各セクションに 1 行 1 項目。

**差し替え範囲**: `xlsx` は各 CSV をシートに対応付ける `sheets` セクション。`pptx` は `range: 4-7` と `keep-layout: yes|no` を持つ `slides` セクション。`docx` は不要。

## 返ってくるもの

child の報告はこのブロック 1 つだけ:

```
STATUS: done | blocked
OUTPUT: <path>
GATES: validate=ok issues=0 gate3=PASS
TOUCHED: <stable path> : <old → new>
BLOCKED: <edit> : <reason> | none
CONTACT_SHEET: <png path> | n/a
DECLARATION: <構成、残したもの、そのまま残せなかったものについて数文>
```

その後、呼び出し側は child 自身が走らせた 2 つの gate コマンド（`officecli validate` と `view issues`）を保存済みファイルに対して再実行し、ブロックの申告と一致するかを報告する。文書についてそれ以上の検査はしない。何が書かれているか、どう見えるかは load した officecli スキルの領分であり、その先は利用者の判断。

`pptx` では contact sheet を見て可読性を判断するよう求められる。呼び出し側はその画像を開かない。判断は利用者のもので、指摘は child に戻される。sheet はスキル同梱の script が枚数から grid を決めて生成するので、行が切れることはない。1 枚に収まらないデッキはスライド順に `-1.png`、`-2.png`、… に分かれる。

## やらないこと

- 内容そのものの検査。child は format に対応する officecli 公式スキルに従い、構成・文言・デザインはそのスキルが所有し、その delivery gate を通す。意図どおりかどうかの判断は、ファイルと contact sheet を見る利用者のもの。
- 呼び出し側は officecli の設計・編集コマンドを自分では実行せず、child のスクリーンショットを読まず、content / original / 指示書を編集しない。child が入力不足を報告したら、入力を直すのは利用者。
- child 1 つはファイル 1 つ。2 ファイルなら 2 回起動する。
