---
name: render-report
description: "Convert a contract-conforming analysis report markdown into pptx, docx, or xlsx via the officecli CLI. docx and xlsx are verbatim transcription. pptx is built by an isolated deck subagent that follows the officecli-pptx skill and may rewrite for the medium; fidelity is enforced afterwards by a deterministic text gate (claim coverage, confidence marks, and a listing of numbers absent from the report), a fresh-context LLM judge and a human read of the contact sheet. The report markdown is never modified. Use when a finished analysis report in markdown must be turned into slides or a document, when the user mentions レポートを pptx に, 資料化, render-report, or asks to convert an analysis report to an Office format."
---

# Render Report

契約準拠の分析レポート markdown を Office 形式に変換する。

**正本の markdown は本スキルが決して書き換えない。** これは全経路で共通する。媒体の都合（スライドに収まらない等）を理由に正本を削らせることもしない。

経路は 2 種類ある。

| 経路 | 誰が作るか | 著述 |
|------|-----------|------|
| `docx` / `xlsx` | 本スキル（1 hop） | 不可。原文転記のみ |
| `pptx` | デッキ subagent [D]（officecli-pptx に従う） | 可。忠実性はゲートが検査 |

pptx を別扱いにするのは、読めるデッキが「媒体向けの再著述」を必要とするため。再著述を禁じたまま忠実に転記すると、節と発見の数だけスライドが積み上がり、忠実だが誰も読まない成果物になる。再著述の自由は [D] に閉じ込め、忠実性は成果物へのゲートで担保する。deck 品質の担い手は render-report ではなく officecli-pptx である。本スキルが持つのは封じ込めと忠実性ゲートだけ。

## Hard Rules

以下は変換品質より優先される。違反した出力は成果物として無効。**適用範囲は経路で変わる。**

| # | docx / xlsx | pptx |
|---|---|---|
| 1 **文言** | 書き換えない。要約・短縮・省略・言い換え・数値の再フォーマット（丸め、桁区切り変更、単位変換）をすべて禁止し、入力の文字列をそのまま転記する | [D] は媒体向けに再著述してよい。ただし **report.md に無い数値を書かない**、**主張を強めない**（様相・順序）。`check_pptx_text.py` が report.md に無い数値を列挙し、[J] がその列挙と本文を読んで判定する |
| 2 **overflow** | 収まらなければ切らずに差し戻す。overflow レポート（対象節・要素・超過の程度）を返し、短縮の判断は内容の所有者が行う | [D] が内部で解決する（箱を広げる・スライドを割る・載せる範囲を削る。文を切り詰めない）。解決できなければ申告する |
| 3 **分析コンテキスト** | 要求しない。入力は markdown 正本と出力パスのみ。読まなければ判断できない状況は markdown の契約違反を意味する（差し戻す） | 同左 |
| 4 **契約違反** | 変換しない。手順 1 の検証が失敗したら違反箇所を返す | 同左 |
| 5 **図** | チャートアノテーションの付いたテーブルからのみ生成する | **report.md にある数値からのみ**図を作る（数値の出所検査が担保）。アノテーションは著者が図として宣言した表であり、それ以外を図にしてもよい |

例外は 1 つだけ: xlsx を別途生成した場合の「全量は別添 xlsx を参照」の一文（[format-mapping.md](references/format-mapping.md) 参照）。

## Inputs

| 入力 | 必須 | 内容 |
|------|------|------|
| markdown 正本のパス | 必須 | 契約準拠のレポート markdown |
| 出力形式 | 必須 | `pptx` / `docx` / `xlsx` のいずれか。複数指定可 |
| 出力先パス | 任意 | 省略時は markdown と同じディレクトリに同名で生成 |
| 範囲 | 任意（pptx のみ） | デッキに載せる部分の指定（「F-6 と F-7 のみ」「主要な発見のみ」）。[D] はその部分だけから作り、G-1 のカバレッジも同じ範囲で検査する。**枚数は指定しない** — 入力を絞る口であって、出力を縛る口ではない |

`xlsx` は明示要求時のみ生成する。`pptx` / `docx` 要求時に自動生成しない。

## Procedure

### 1. 契約検証（省略禁止・全形式共通）

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/validate_contract.py path/to/report.md
```

`OK` 以外が返ったら **変換に進まない**。違反行の一覧をそのまま呼び出し元に返し、修正後の再実行を求める。

### 2. officecli の確認（全形式共通）

```bash
officecli --version
```

未インストールなら:

- Windows: `irm https://d.officecli.ai/install.ps1 | iex`
- macOS / Linux: `curl -fsSL https://d.officecli.ai/install.sh | bash`

コマンド構文・プロパティ名・列挙値が不確かなときは推測せず help を実行する（`officecli help pptx`、`officecli help pptx add shape` 等）。インストール済み CLI の help が権威。

### 3. 形式ごとの手順

- `docx` / `xlsx` → [format-mapping.md](references/format-mapping.md) の該当節に従って 1 hop で生成する。以降の手順 4-7 は pptx 専用。
- `pptx` → 手順 4 へ。

### 4. pptx: [D] の起動

デッキ subagent [D] を 1 つ起動する。手順は [deck-building.md](references/deck-building.md)。

| 役割 | 読むもの | 出すもの | してはいけないこと |
|------|---------|---------|-------------------|
| **[D] deck-builder** | `~/.claude/skills/officecli-pptx/SKILL.md`（全文）、report.md（全文）、[deck-building.md](references/deck-building.md)、範囲（あれば） | pptx ＋ contact sheet ＋ 申告 | report.md を書き換える。画像を呼び出し元へ返す。Gate 3 を自分で判定する |

[D] は officecli-pptx の QA ゲート（validate / view issues / placeholder 走査 / screenshot 視覚監査、fix-verify 最大 3 巡）を**自分のコンテキスト内で**回し、Gate 3 の判定を fresh subagent（監査役）に委ねる。呼び出し元は contact sheet のパスと申告だけを受け取る。

本スキル自身が subagent として起動されている場合も同じ。[D] を自分の内側で起動する。

### 5. pptx: G-1（決定論ゲート。[D] の申告を信用せず再実行）

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_pptx_text.py path/to/deck.pptx path/to/report.md [--scope F-6,F-7]
officecli validate path/to/deck.pptx        # スキーマ。PowerPoint で開けることは証明しない
officecli view path/to/deck.pptx issues     # overflow / はみ出し / 構造。1 件でも出たら [D] へ差し戻す
```

`check_pptx_text.py` は 2 点を判定する: 範囲内の全 `F-n` が面に存在、各 `F-n` がその確信度記号と同じスライドに載る。違反があれば違反行を添えて [D] に差し戻す（外側 1 回まで）。

加えて **report.md に無い数値を列挙する**（判定はしない）。装飾番号（区切りの `01`）も派生値（`88.30%` = 100 − 11.70）も同じ列に並ぶので、どれがデータ値かは機械では決められない。この列挙は [J] に渡す（手順 6）。

加えてコントラスト検査（下記）。

### コントラスト検査

背景色または文字色を明示指定した箇所は、テーマ既定の配色から外れて文字がつぶれうる。該当箇所の色を読み出して判定する。

```bash
officecli get deck.pptx "/slide[N]" --json          # background（明示指定時のみ現れる）
officecli get deck.pptx "/slide[N]/shape[@id=ID]" --json   # color（明示指定時のみ現れる）
```

読み出した色で判定する。

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_contrast.py "slide6/body:#2B2B2B:#1F3864:14"
```

判定基準は WCAG 2.x のコントラスト比（通常テキスト 4.5:1、大きいテキスト 3.0:1）。`FAIL` が出た箇所は [D] に差し戻す。

`background` も `color` も読み出しに現れない場合、その要素はテーマ既定の配色に従っており、検査は不要。

`officecli view <pptx> issues --type low_contrast` はこの手順の代替にならない（測定結果は `docs/skills/render-report/AUTHORING_CONTRACT.md`）。

### 6. pptx: G-2 忠実性ジャッジ [J]

G-1 が通ったら [J] を **fresh context** で起動する。手順は [deck-review.md](references/deck-review.md)。

| 役割 | 読むもの | 出すもの | してはいけないこと |
|------|---------|---------|-------------------|
| **[J] deck-judge** | report.md 全文、`officecli view deck.pptx text` の出力、`check_pptx_text.py` が列挙した「report.md に無い数値」、範囲（あれば）、[deck-review.md](references/deck-review.md) | `PASS` / `FAIL` ＋所見 | 編集する。可読性を評価する。画像を見る |

先頭行が `FAIL` なら所見を添えて [D] に差し戻す。model は指定せず親を継承する。

### 7. pptx: 人間承認 [H]

contact sheet（[D] が返したパス）を人間に見せ、**可読性を判断してもらう**。忠実性は G-1 / G-2 が済ませている。指摘はすべて [D] へ戻す（表現も内容も宛先は同じ）。

## ループ

```
[D] → G-1 → [J] → [H]
 ▲     │      │      │
 └─────┴──────┴──────┘  FAIL / 指摘 → [D] へ（所見を添える）
```

- G-1 の FAIL → [D]。外側 1 回まで。
- [J] の FAIL → [D]。2 巡まで。3 巡目に入る前にユーザへ escalation する。
- [H] の指摘 → [D]。再入した [D] の出力には G-1 → [J] を再実行する。

## Output

- 生成ファイルのパス
- 検査結果（G-1 / [J] の判定、`view issues` の件数、コントラスト）
- pptx の場合、contact sheet のパス、枚数、[D] の構成申告
- 契約検証で差し戻した場合は違反一覧

## Invocation

**直接起動**: markdown パスと形式（と任意の範囲）を引数として受け取る。

**委譲起動**: 呼び出し元が本 SKILL.md のパス・markdown 正本のパス・形式・出力先（・範囲）を渡す。分析コンテキストは渡さない（Hard Rule 3）。docx / xlsx は subagent 1 つで完結する。pptx は本スキルが [D] と [J] を順に起動する。

差し戻し（契約違反）を受けた呼び出し元は、markdown を修正してから再度変換を依頼する。本スキルが markdown を書き換えることはない。
