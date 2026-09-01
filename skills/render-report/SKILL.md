---
name: render-report
description: "Convert a contract-conforming analysis report markdown into pptx, docx, or xlsx via the officecli CLI. docx and xlsx are verbatim transcription. pptx goes through a deck-spec: an authoring hop that may rewrite for the medium, gated by a deterministic contract check, an LLM fidelity judge and a human read, then a transcription hop that may not rewrite anything. The report markdown is never modified. Use when a finished analysis report in markdown must be turned into slides or a document, when the user mentions レポートを pptx に, 資料化, render-report, or asks to convert an analysis report to an Office format."
---

# Render Report

契約準拠の分析レポート markdown を Office 形式に変換する。

**正本の markdown は本スキルが決して書き換えない。** これは全経路で共通する。媒体の都合（スライドに収まらない等）を理由に正本を削らせることもしない。

経路は 2 種類ある。

| 経路 | hop | 著述 |
|------|-----|------|
| `docx` / `xlsx` | report.md → 出力（1 hop） | 不可。原文転記のみ |
| `pptx` | report.md → deck-spec → pptx（2 hop） | hop1 のみ可。hop2 は原文転記のみ |

pptx だけが 2 hop なのは、読めるデッキが「媒体向けの再著述」を必要とするため。再著述を禁じたまま忠実に転記すると、節と発見の数だけスライドが積み上がり、忠実だが誰も読まない成果物になる。再著述の自由は hop1 に閉じ込め、忠実性はゲートで担保する。

## Hard Rules

以下は変換品質より優先される。違反した出力は成果物として無効。**適用範囲は hop 単位**で、hop ごとに誰が何をしてよいかが変わる。

1. **文言を書き換えない（hop2、および docx / xlsx の全体）**。要約・短縮・省略・言い換え・数値の再フォーマット（丸め、桁区切り変更、単位変換）をすべて禁止する。入力の文字列をそのまま転記する。
   - **hop1（report.md → deck-spec）はこの規則の対象外**。再著述が hop1 の目的である。hop1 の忠実性は [deck-authoring.md](references/deck-authoring.md) のルールと、G-A / G-B / 人間承認の 3 ゲートが担保する。
2. **収まらなければ切らずに差し戻す**。テキストが領域に収まらない場合、truncate も要約もせず overflow レポート（対象スライド/節・要素・超過の程度）を返す。
   - docx / xlsx: 呼び出し元に差し戻す。短縮の判断は内容の所有者が行う。
   - pptx: **deck-spec を短縮する**（[A] の職分）。report.md には触れない。
3. **分析コンテキストを要求しない**。入力は markdown 正本と出力パスのみ。分析の中間生成物（ハンドオフ、統計出力、生データ）を読まない。読まなければ判断できない状況は、markdown が契約違反であることを意味する（差し戻す）。
4. **契約違反を変換しない**。手順 1 の検証が失敗したら変換に進まず、違反箇所を返す。
5. **図は宣言されたものだけ**。チャートアノテーションの付いたテーブルからのみチャートを生成する。markdown にない図を作らない。deck-spec 側でチャートを発明しない。

例外は 1つだけ: xlsx を別途生成した場合の「全量は別添 xlsx を参照」の一文（[format-mapping.md](references/format-mapping.md) 参照）。

## Inputs

| 入力 | 必須 | 内容 |
|------|------|------|
| markdown 正本のパス | 必須 | 契約準拠のレポート markdown |
| 出力形式 | 必須 | `pptx` / `docx` / `xlsx` のいずれか。複数指定可 |
| 出力先パス | 任意 | 省略時は markdown と同じディレクトリに同名で生成 |

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

- `docx` / `xlsx` → [format-mapping.md](references/format-mapping.md) の該当節に従って 1 hop で生成する。以降の手順 4-8 は pptx 専用。
- `pptx` → 手順 4 へ。

### 4. pptx: 3 役割の起動

pptx 経路は 3 つの役割を**順に**実行する。役割はそれぞれ独立したコンテキストで動く。

**subagent から subagent を起動することはできない。** したがって 3 役割の起動主体は必ず呼び出し元のメインスレッド（分析パイプラインのオーケストレータ、または `/render-report` 直接起動時のメイン）である。本スキルが subagent として起動されている場合、3 役割を自分で起動せず、**本節の起動仕様をメインスレッドに返して終了する**。

| 役割 | 読むもの | 出すもの | してはいけないこと |
|------|---------|---------|-------------------|
| **[A] deck-author** | report.md 全文、[deck-authoring.md](references/deck-authoring.md)、[deck-md-contract.md](references/deck-md-contract.md)、[deck-archetypes.md](references/deck-archetypes.md)（容量目安のみ） | deck-spec の markdown 1 本 | officecli を触る。pptx を生成する |
| **[G-B] deck-judge** | report.md 全文、deck-spec の本文スライド、[deck-review.md](references/deck-review.md) | `PASS` / `FAIL` ＋所見 | 編集する。可読性を評価する |
| **[R] deck-renderer** | deck-spec、[deck-archetypes.md](references/deck-archetypes.md) | pptx | report.md を読む。文言を書き換える |

[G-B] は **fresh context** で起動する（[A] と同じコンテキストで採点させない）。model は指定せず親を継承する。

### 5. pptx: G-A（決定論ゲート）

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/validate_deck.py path/to/deck-spec.md path/to/report.md
```

構造・数値の出所・source 表の一致・チャート・主張カバレッジ・禁止語を検査する。`OK` 以外なら違反行を添えて [A] に差し戻す。

### 6. pptx: G-B と人間承認

G-A が通ったら [G-B] を起動する。先頭行が `FAIL` なら所見を添えて [A] に差し戻す。

`PASS` なら **deck-spec のテキストを人間に提示して承認を得る**。ここで判断するのは可読性であり、忠実性ではない（それは G-A / G-B が済ませている）。承認前に [R] を起動しない。

### 7. pptx: レンダリング

[R] を起動する。[deck-archetypes.md](references/deck-archetypes.md) の固定表に従って配置する。配置をスライドごとに考えない。構造操作（スライド追加、チャート追加）のたびに結果を確認してから次に進む。

### 8. pptx: G-R（hop2 の検査）

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_pptx_text.py path/to/deck.pptx path/to/deck-spec.md
```

スライド上の全テキストが deck-spec に由来することを検査する。加えて次を確認する。

- テキストの overflow（領域外にはみ出した要素）
- チャートの系列・カテゴリがアノテーションの `x=` / `y=` と一致するか
- 文字色と背景色のコントラスト（下記）
- `officecli validate` が通ること。ただし validate は PowerPoint で開けることを証明しない

overflow を検出したら [A] に差し戻す（**report.md ではなく deck-spec を短縮する**）。

### コントラスト検査

背景色または文字色を明示指定した箇所は、テーマ既定の配色から外れて文字がつぶれうる。該当箇所の色を読み出して判定する。

```bash
officecli get report.pptx "/slide[N]" --json          # background（明示指定時のみ現れる）
officecli get report.pptx "/slide[N]/shape[@id=ID]" --json   # color（明示指定時のみ現れる）
```

読み出した色で判定する。

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/check_contrast.py "slide6/body:#2B2B2B:#1F3864:14"
```

判定基準は WCAG 2.x のコントラスト比（通常テキスト 4.5:1、大きいテキスト 3.0:1）。`FAIL` が出た箇所は色を修正して再検査する。**色の修正は文言の変更ではないため Hard Rule 1 に抵触しない**。

`background` も `color` も読み出しに現れない場合、その要素はテーマ既定の配色に従っており、検査は不要。

`officecli view <pptx> issues --type low_contrast` はこの手順の代替にならない（測定結果は `docs/skills/render-report/AUTHORING_CONTRACT.md`）。

## ループ

```
[A] → G-A → [G-B] → 人間承認 → [R] → G-R
       │       │                        │
       └───────┴── FAIL / overflow ─────┘  → [A] へ（所見を添える）
```

- 上限 **2 巡**。3 巡目に入る前にユーザへ escalation する。
- overflow による再入は、[A] が該当スライドを短縮 → G-A → **G-B を deck-spec 全体に対して再実行** → 人間承認は省略 → [R]。短縮は `QUALIFIER_DROP` が最も起きる操作なので、G-B の再実行を省略しない。
- overflow 再入の回数は上記の上限に合算する。

## Output

- 生成ファイルのパス
- 検査結果（G-A / G-B / G-R の判定、overflow の有無）
- pptx の場合、承認された deck-spec のパス
- 契約検証で差し戻した場合は違反一覧

## Invocation

**直接起動**: markdown パスと形式を引数として受け取る。

**委譲起動**: 呼び出し元が本 SKILL.md のパス・markdown 正本のパス・形式・出力先を渡す。分析コンテキストは渡さない（Hard Rule 3）。docx / xlsx は subagent 1 つで完結する。pptx は手順 4 のとおりメインスレッドが 3 役割を順に起動する。

差し戻し（契約違反）を受けた呼び出し元は、markdown を修正してから再度変換を依頼する。本スキルが markdown を書き換えることはない。
