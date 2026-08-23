---
name: render-report
description: "Convert a contract-conforming analysis report markdown into pptx, docx, or xlsx via the officecli CLI, without rewriting any wording. Validates the markdown against the report contract first, renders layout only, and reports overflow instead of truncating text. Use when a finished analysis report in markdown must be turned into slides or a document, when the user mentions レポートを pptx に, 資料化, render-report, or asks to convert an analysis report to an Office format."
---

# Render Report

契約準拠の分析レポート markdown を Office 形式に変換する。**内容の著者ではなく、配置の実行者**として動作する。

## Hard Rules

以下は変換品質より優先される。違反した出力は成果物として無効。

1. **文言を書き換えない**。要約・短縮・省略・言い換え・数値の再フォーマット（丸め、桁区切り変更、単位変換）をすべて禁止する。markdown の文字列をそのまま転記する。
2. **収まらなければ切らずに差し戻す**。テキストが領域に収まらない場合、truncate も要約もせず、overflow レポート（対象スライド/節・要素・超過の程度）を返し、その箇所を未完成と明示する。短縮の判断は内容の所有者（呼び出し元）が行う。
3. **分析コンテキストを要求しない**。入力は markdown 正本と出力パスのみ。分析の中間生成物（ハンドオフ、統計出力、生データ）を読まない。読まなければ判断できない状況は、markdown が契約違反であることを意味する（差し戻す）。
4. **契約違反を変換しない**。手順 1 の検証が失敗したら変換に進まず、違反箇所を返す。
5. **図は宣言されたものだけ**。チャートアノテーションの付いたテーブルからのみチャートを生成する。markdown にない図を作らない。

例外は 1つだけ: xlsx を別途生成した場合の「全量は別添 xlsx を参照」の一文（[format-mapping.md](references/format-mapping.md) 参照）。

## Inputs

| 入力 | 必須 | 内容 |
|------|------|------|
| markdown 正本のパス | 必須 | 契約準拠のレポート markdown |
| 出力形式 | 必須 | `pptx` / `docx` / `xlsx` のいずれか。複数指定可 |
| 出力先パス | 任意 | 省略時は markdown と同じディレクトリに同名で生成 |

`xlsx` は明示要求時のみ生成する。`pptx` / `docx` 要求時に自動生成しない。

## Procedure

### 1. 契約検証（省略禁止）

```bash
uv run --script ${CLAUDE_SKILL_DIR}/scripts/validate_contract.py path/to/report.md
```

`OK` 以外が返ったら **変換に進まない**。違反行の一覧をそのまま呼び出し元に返し、修正後の再実行を求める。

### 2. 変換規則の読み込み

[format-mapping.md](references/format-mapping.md) を読む。要求された形式の節のみ読めばよい。

構造契約そのものの定義は [report-md-contract.md](references/report-md-contract.md) にある。検証が通った markdown を扱う限り、通常は読む必要がない。

### 3. officecli の確認

```bash
officecli --version
```

未インストールなら:

- Windows: `irm https://d.officecli.ai/install.ps1 | iex`
- macOS / Linux: `curl -fsSL https://d.officecli.ai/install.sh | bash`

コマンド構文・プロパティ名・列挙値が不確かなときは推測せず help を実行する（`officecli help pptx`、`officecli help pptx add shape` 等）。インストール済み CLI の help が権威。

pptx を生成する場合、`~/.claude/skills/officecli-pptx/SKILL.md` が存在すれば deck 体裁の要件として読む（存在しなければ `officecli help pptx` で代替）。

### 4. 変換

format-mapping.md の対応表に従って生成する。構造操作（スライド追加、チャート追加）のたびに結果を確認してから次に進む。一括投入して最後にまとめて確認しない。

### 5. 検査と報告

生成後に以下を確認する。

- テキストの overflow（領域外にはみ出した要素）
- 文言の一致（markdown の原文と生成物のテキストが一致するか。特に金額・パーセント・n 数）
- チャートの系列・カテゴリがアノテーションの `x=` / `y=` と一致するか
- **文字色と背景色のコントラスト**（下記）

### コントラスト検査

背景色または文字色を明示指定した箇所は、テーマ既定の配色から外れるため文字がつぶれうる。該当箇所の色を読み出して判定する。

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

overflow を検出した場合は Hard Rule 2 に従い、以下の形式で報告する。

```
OVERFLOW
- slide 4 / body text: 約 1.4 倍の分量
- slide 7 / table: 行数超過（12行、収容可能 8行）
対応: 該当箇所は未完成。短縮方針の指示を待つ。
```

## Output

- 生成ファイルのパス
- 検査結果（overflow の有無、文言一致の確認結果）
- 契約検証で差し戻した場合は違反一覧

## Invocation

**直接起動**: markdown パスと形式を引数として受け取る。

**委譲起動**: 呼び出し元（分析パイプラインのオーケストレータ等）が本 SKILL.md のパス・markdown 正本のパス・形式・出力先を渡し、subagent に読ませて実行させる。分析コンテキストは渡さない（Hard Rule 3）。

差し戻し（契約違反・overflow）を受けた呼び出し元は、markdown を修正してから再度変換を依頼する。本スキルが markdown を書き換えることはない。
