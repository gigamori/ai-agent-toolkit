# Format Mapping

契約準拠 markdown（[report-md-contract.md](report-md-contract.md)）から各形式への変換規則。

共通する原則: **正本の markdown は書き換えない**。収まらない場合も truncate しない（SKILL.md の overflow 手順に従う）。

原文転記の適用範囲は経路で異なる。docx / xlsx は全体が原文転記。pptx はデッキ subagent [D] が再著述してよく、忠実性は生成後のゲート（SKILL.md 手順 5-7）が検査する。

## 変換対象の対応

| markdown 要素 | pptx（[D] が決める。目安） | docx | xlsx（明示要求時のみ） |
|---------------|---------------------------|------|------------------------|
| frontmatter `title` | 表紙 | 文書タイトル | ブック名 |
| エグゼクティブサマリ | 冒頭のスライド | 冒頭節 | 対象外 |
| 分析対象データ | スライド | 節 | 対象外 |
| データの基本的特徴 | スライド | 節 | 対象外 |
| クレームブロック | 発見のスライド（`F-n` と確信度記号を同じ面に） | 各 1節 | 対象外 |
| 限界と注意事項 | スライド | 節 | 対象外 |
| 付録: 再現性 / 変数対応表 | 付録スライド | 末尾節 | 対象外 |
| 付録: 検証詳細 | 付録スライド | 末尾節 | 対象外 |
| アノテーション付きテーブル | チャート | チャート埋め込み | 1テーブル = 1シート |
| アノテーションなしテーブル | 表、または図 | 表として配置 | 1テーブル = 1シート |

## pptx

pptx は転記ではない。デッキ subagent [D] が report.md を読み、officecli-pptx SKILL.md を標準として pptx を作る。**この節は markdown 要素とスライドの直接対応を定義しない** — 構成は [D] が決める。上の対応表の pptx 列は「載りうる場所」の目安であり、[D] を縛らない。

```
report.md ──[D]──▶ pptx ──▶ G-1 / [J] / [H]
       再著述してよい      忠実性はここで検査
```

- [D] の手順は [deck-building.md](deck-building.md)。読むのは `~/.claude/skills/officecli-pptx/SKILL.md`、report.md、deck-building.md。
- 数値は report.md にあるものだけ（`check_pptx_text.py` が検査）。n と分母を落とさない、様相を強めない、順序を変えない（[J] が検査）。
- report.md は書き換えない。overflow は [D] が内部で解決する。

### チャート

report.md にある数値からのみ作る。アノテーション付きテーブルは著者が図として宣言したものなので、`x=` をカテゴリ軸、`y=` を系列、`title=` をタイトルとして使う。アノテーションの無い数値列（発見の根拠にある推移など）を図にしてもよい。種別は officecli-pptx の chart-choice 表に従う。

### batch 実行時の注意

`officecli batch` は atomic（1件でも失敗すると全件ロールバックされ、ファイルは無変更のまま）。スライドを追加しながら同じ batch 内でその子要素を追加する場合、`parent` のスライド番号は**その時点までに追加済みのスライド数**で決まる。追加順を数えずに最終的な番号を書くと `Slide N not found` で全体が巻き戻る。

### 体裁

officecli-pptx SKILL.md の deck 要件に従う。render-report はその上に体裁の規則を足さない。

背景色や文字色を明示指定した場合は、テーマ既定の配色から外れて文字がつぶれうる。生成後に SKILL.md の「コントラスト検査」で判定する（`scripts/check_contrast.py`）。色を指定しない場合はテーマの配色が保たれるため検査は不要。

## docx

docx は markdown を一括投入する。要素を1つずつ組み立てない。

`markdown` 要素（docx 専用。pptx / xlsx には無い）が markdown を編集可能なネイティブ Word 要素へ展開する。見出し階層・箇条書き・GFM テーブル・強調はそのまま構造が保たれる。**文言の転記を CLI が行うため、Hard Rule 1（文言を書き換えない）が構造的に保証される**。

### 手順

1. **前処理**: frontmatter とチャートアノテーションを除去した本文を作る。どちらも除去しないと**可視テキストとして本文に混入する**（`---` は区切り線、YAML 行は段落、HTML コメントは段落になる）。

   ```bash
   awk 'NR==1 && $0=="---" {fm=1; next} fm==1 && $0=="---" {fm=2; next} fm==1 {next} /^<!-- chart:/ {next} {print}' report.md > body.md
   ```

   除去したチャートアノテーションは、後段で使うため「何番目のテーブルの直前にあったか」を控える。

2. **見出しスタイルの定義**: `create` 直後の docx には Heading スタイルが無く、投入時に `style 'Heading1' not found` の警告が出て見出しが本文と同じ体裁になる。投入前に定義する。

   ```bash
   officecli add report.docx /styles --type style --prop id=Heading1 --prop name="見出し 1" \
     --prop type=paragraph --prop basedOn=Normal --prop size=18 --prop bold=true
   ```

   Heading1 / Heading2 / Heading3 の3つを定義すれば、契約のセクション階層をすべて賄える。

3. **一括投入**:

   ```bash
   officecli add report.docx / --type markdown --prop src=body.md
   ```

4. **チャートの挿入**: 段落を追加して chart を格納し、対応するテーブルの直後へ移動する。`add` は本文末尾に追加されるため、`move --after` が必要。

   ```bash
   officecli add report.docx /body --type paragraph --prop text=""
   officecli add report.docx "/body/p[@paraId=PARA_ID]" --type chart \
     --prop chartType=bar --prop categories="google,direct" --prop data="cvr:12.4,6.1"
   officecli move report.docx "/body/p[@paraId=PARA_ID]" --after "/body/tbl[1]"
   ```

   テーブルは本文の出現順に `/body/tbl[N]` となる。

5. **確認**: `validate` に加えて、`view text` に frontmatter 由来の行とアノテーション文字列が残っていないことを確認する。

### 既知の制約（officecli の markdown 展開）

- リンクと画像は可視テキストに退化する（`[t](u)` は `t`、`![a](u)` は `!a`。ハイパーリンク関係も埋め込み画像も生成されない）
- テーブルのセルはプレーンテキストになる（セル内のインライン書式は落ちる）
- 入れ子の引用は1段に平坦化される

契約準拠のレポートは本文にリンクも画像も要求せず、テーブルはプレーンな数値表であるため、上記の制約は実害を生じない。制約に触れる markdown が来た場合は変換を止めず、影響箇所を報告する。

## xlsx

明示要求時のみ生成する。レポート本文は xlsx にしない。用途はデータ付録。

- markdown 内の GFM テーブル 1つ = 1シート
- シート名はテーブル直前の見出しから生成（31文字上限、使用不可文字は除去）
- 1行目をヘッダ行として固定
- 生成した場合、pptx/docx 側の該当テーブルに「全量は別添 xlsx を参照」と記す（この一文の追加は許可される唯一の文言追加）

## officecli の参照

コマンド構文・プロパティ名・列挙値が不確かな場合は推測せず `officecli help` 系を実行する。インストール済み CLI の help が権威。
