# Format Mapping

契約準拠 markdown（[report-md-contract.md](report-md-contract.md)）から各形式への変換規則。

共通する原則: **正本の markdown は書き換えない**。収まらない場合も truncate しない（SKILL.md の overflow 手順に従う）。

原文転記の適用範囲は経路で異なる。docx / xlsx は 1 hop で全体が原文転記。pptx は deck-spec を挟む 2 hop で、著述が許されるのは hop1（report.md → deck-spec）だけであり、hop2（deck-spec → pptx）は原文転記のみ。

## 変換対象の対応

| markdown 要素 | pptx（deck-spec の原型） | docx | xlsx（明示要求時のみ） |
|---------------|--------------------------|------|------------------------|
| frontmatter `title` | `title` | 文書タイトル | ブック名 |
| エグゼクティブサマリ | `exec-summary` | 冒頭節 | 対象外 |
| 分析対象データ | `section` | 節 | 対象外 |
| データの基本的特徴 | `section` | 節 | 対象外 |
| クレームブロック | `finding`（+ チャートがあれば `evidence`） | 各 1節 | 対象外 |
| 限界と注意事項 | `section` | 節 | 対象外 |
| 付録: 再現性 / 変数対応表 | `appendix` | 末尾節 | 対象外 |
| 付録: 検証詳細 | `appendix` | 末尾節 | 対象外 |
| アノテーション付きテーブル | `evidence`（チャート + source 表） | チャート埋め込み | 1テーブル = 1シート |
| アノテーションなしテーブル | `section` の表、または `evidence` の source 表 | 表として配置 | 1テーブル = 1シート |

## pptx

pptx だけが 2 hop である。**この節は markdown 要素とスライドの直接対応を定義しない** — その対応は deck-spec 経由でのみ成立する。

```
report.md ──[A]──▶ deck-spec ──[R]──▶ pptx
           hop1              hop2
        再著述してよい      原文転記のみ
```

### hop1: report.md → deck-spec

デッキ著述役 [A] が行う。規則は [deck-authoring.md](deck-authoring.md)、出力の構造は [deck-md-contract.md](deck-md-contract.md)。

要点だけ再掲する（詳細を本ファイルに複製しない）:

- 再著述してよい。ただし数値は原文転記、n と分母を落とさない、様相を強めない、順序を変えない。
- report.md は書き換えない。overflow の短縮は deck-spec 側で行う。
- チャートは report.md のアノテーションを原文のまま写したものだけ。

### hop2: deck-spec → pptx

レンダリング役 [R] が行う。**[R] は report.md を読まない。** 読むのは deck-spec と [deck-archetypes.md](deck-archetypes.md) だけ。

- 配置は deck-archetypes.md の固定表に従う。スライドごとに領域を考えない。
- deck-spec の文字列をそのまま転記する。Hard Rule 1 がここに適用される。
- スライド面に置いてよい deck-spec 由来でない文字列は、deck-archetypes.md が列挙する 3 語のラベルとページ番号だけ。
- 収まらない場合は短縮せず overflow として差し戻す。

### チャート

アノテーションの種別を officecli のチャート種別に対応させる。`x=` をカテゴリ軸、`y=` を系列とする。`y=` が複数列なら複数系列。`title=` があればチャートタイトルに使う。

チャート化した元テーブルは同じ `evidence` スライドに source 表として残す（数値の検証可能性を保つため）。

### batch 実行時の注意

`officecli batch` は atomic（1件でも失敗すると全件ロールバックされ、ファイルは無変更のまま）。スライドを追加しながら同じ batch 内でその子要素を追加する場合、`parent` のスライド番号は**その時点までに追加済みのスライド数**で決まる。追加順を数えずに最終的な番号を書くと `Slide N not found` で全体が巻き戻る。

### 体裁

officecli-pptx スキルの deck 要件（1スライド1メッセージ、明示的な型階層、テーマ既定に依存しないサイズ指定）に従う。deck-archetypes.md の固定表はこの要件を満たすように組んである。

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
