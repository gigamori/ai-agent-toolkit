# Deck Archetypes

deck-spec の 6 原型を pptx 上のどこに置くかの固定表。**レンダリング役 [R] が読む唯一の配置定義**であり、[R] はこの表と deck-spec 以外を読まない（report.md を読まない）。

配置を毎回考えないためにここで固定する。スライドごとに領域を発明すると、同じ原型のスライドが揃わず、overflow の再現条件も追えなくなる。

## キャンバス

| 項目 | 値 |
|------|-----|
| スライドサイズ | 33.87cm × 19.05cm（16:9） |
| 左バンド | `x=0cm y=0cm w=0.55cm h=19.05cm`、塗り `#1E2761` |
| 本文の左端 | `x=2.3cm` |
| 本文の幅 | `w=30.07cm` |
| ページ番号 | `x=31cm y=17.85cm w=1.4cm h=0.9cm`、10pt |
| 和文フォント | Meiryo |

色を明示指定した場合は SKILL.md のコントラスト検査を通す。指定しなければテーマ既定の配色が保たれ、検査は不要。

## 原型別の領域

すべて `x=2.3cm w=30.07cm`。以下は `y` / `h` / フォントサイズ / 太字 のみを示す。

### title

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| タイトル（frontmatter `title`） | 6.5cm | 3.2cm | 32pt | true |
| サブ情報（`source`） | 10.0cm | 1.0cm | 14pt | false |

ページ番号を置かない。

### exec-summary

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| 見出し（原型名の訳語ではなく `trace` の節名） | 0.95cm | 1.64cm | 26pt | true |
| `message` | 3.14cm | 2.2cm | 16pt | false |
| `findings` のラベル（`主要な発見`） | 5.6cm | 1.0cm | 14pt | true |
| `findings` | 6.65cm | 5.6cm | 16pt | false |
| `limits` のラベル（`主な限界`） | 12.5cm | 1.0cm | 14pt | true |
| `limits` | 13.55cm | 3.6cm | 14pt | false |

### section

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| 見出し（`trace` の節名） | 0.95cm | 1.64cm | 26pt | true |
| `headline` | 3.14cm | 1.6cm | 20pt | true |
| `body` | 5.0cm | 4.4cm | 16pt | false |
| テーブル（`table` 宣言があるとき） | 9.7cm | 7.4cm | 12pt | false |

テーブルが無い場合、`body` の h を 12.1cm まで伸ばす。

### finding

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| 見出し（`trace` の `F-n`） | 0.95cm | 1.0cm | 14pt | true |
| `headline` | 2.1cm | 2.2cm | 24pt | true |
| `confidence` | 4.5cm | 0.9cm | 14pt | false |
| `body` | 5.7cm | 4.6cm | 16pt | false |
| `evidence` | 10.6cm | 4.0cm | 14pt | false |
| `limit` | 14.9cm | 2.2cm | 12pt | false |

`limit` が無い場合、`evidence` の h を 6.2cm まで伸ばす。
`notes` は発表者ノートへ入れる（スライド面に描画しない）。

### evidence

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| 見出し（`<trace> 根拠`） | 0.95cm | 1.0cm | 14pt | true |
| チャート | 2.2cm | 7.6cm | — | — |
| source 表 | 10.2cm | 6.8cm | 12pt | false |

チャートアノテーションが無いスライドでは、source 表の y を 2.2cm、h を 14.8cm にする。

## スライド面に置いてよい、deck-spec 由来でない文字列

hop2 の文言一致検査（`scripts/check_pptx_text.py`）は、スライド上の全テキストが deck-spec に由来することを要求する。例外はここに列挙した 3 語のラベルとページ番号だけであり、[R] はこれ以外の語を足せない。

| 追加してよい文字列 | 置き場所 |
|-------------------|---------|
| `主要な発見` | `exec-summary` の `findings` ラベル |
| `主な限界` | `exec-summary` の `limits` ラベル |
| `根拠` | `evidence` の見出しで `<trace> 根拠` の形に限る |
| スライド番号（数字のみ） | ページ番号 |

見出しに使う節名・`F-n` は deck-spec の `trace` 値そのもの、表紙のタイトルと副題は frontmatter の `title` / `source` そのものであり、いずれも「足した文字列」ではない。

### appendix

| 要素 | y | h | size | bold |
|------|---|---|------|------|
| 見出し（`trace`） | 0.95cm | 1.64cm | 26pt | true |
| 本文 / テーブル | 3.14cm | 14.0cm | 12pt | false |

## 容量目安

和文の全角 1 文字の送り幅をフォントサイズと等しいとみなした概算。行間 1.2 倍。**目安であって保証ではない**。実際の overflow 判定は生成後の検査（G-R）で行う。

| size | 1 行の全角文字数（w=30.07cm） | 行の高さ |
|------|------------------------------|---------|
| 12pt | 71 | 0.51cm |
| 14pt | 60 | 0.59cm |
| 16pt | 53 | 0.68cm |
| 20pt | 42 | 0.85cm |
| 24pt | 35 | 1.02cm |
| 26pt | 32 | 1.10cm |

領域に入る概算文字数 = `1 行の文字数 × floor(h / 行の高さ)`。主要スロットの目安:

| 原型 / スロット | size | 概算上限 |
|----------------|------|---------|
| `exec-summary` / `message` | 16pt | 159 |
| `exec-summary` / `findings` | 16pt | 424 |
| `exec-summary` / `limits` | 14pt | 360 |
| `section` / `headline` | 20pt | 42 |
| `section` / `body` | 16pt | 318 |
| `finding` / `headline` | 24pt | 70 |
| `finding` / `body` | 16pt | 318 |
| `finding` / `evidence` | 14pt | 360 |
| `finding` / `limit` | 12pt | 213 |

超過を見つけた [R] は、**短縮せず** overflow として差し戻す（SKILL.md の Hard Rule 2）。短縮はデッキ著述役 [A] の職分。

## 表の行数

12pt・行の高さ 0.6cm で、`section` のテーブル領域（7.4cm）に**ヘッダ + 11 行**、`evidence` のテーブル領域（6.8cm）に**ヘッダ + 10 行**、チャートなしの `evidence`（14.8cm）に**ヘッダ + 23 行**が目安。source 表がこれを超える場合、行を間引かず 2 枚の `evidence` スライドに分ける。
