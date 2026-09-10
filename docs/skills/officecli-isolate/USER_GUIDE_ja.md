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
DEGRADED: <取込が運べなかったもの> | none
DRIFT: <object> : <old range → new range> : extended|left | none
DECLARATION: <構成、残したもの、落としたものについて数文>
```

その後、呼び出し側がファイルに対して `officecli validate` と `view issues` を再実行し、`check_office_text.py` を走らせる。この script はファイルの text を読み、content と照合する（`docx` / `xlsx` は全行と全数値、`pptx` は全数値。見つからなかった見出しと正本に無い数値は判定せず一覧として示す）か、指示書の value と照合する（部分更新）。FAIL は違反行付きで child に 1 回だけ差し戻され、2 回目の FAIL は利用者に報告される。

`pptx` では contact sheet を見て可読性を判断するよう求められる。呼び出し側はその画像を開かない。判断は利用者のもので、指摘は child に戻される。sheet はスキル同梱の script が枚数から grid を決めて生成するので、行が切れることはない。1 枚に収まらないデッキはスライド順に `-1.png`、`-2.png`、… に分かれる。

## やらないこと

- 忠実性照合が読むのは text、表セル、chart データ、セルの生値。画像・speaker notes・書式は読まないので、画像にしか無い値は照合されない。
- 呼び出し側は officecli の設計・編集コマンドを自分では実行せず、child のスクリーンショットを読まず、content / original / 指示書を編集しない。child が入力不足を報告したら、入力を直すのは利用者。
- child 1 つはファイル 1 つ。2 ファイルなら 2 回起動する。
