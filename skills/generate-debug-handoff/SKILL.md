---
name: generate-debug-handoff
description: E2Eテスト用の debug handoff Markdown を生成する。debugger引数(human/llm)必須。debugger:humanではLLMは整形補助役で人間が承認、debugger:llmではLLMがdebugger役で承認なし。「debug handoff」「テストhandoff」「E2Eテスト表」に言及がある場合に使用。
---

# Debug Handoff Generation

## Overview

E2Eテストの不具合切り分け用 debug handoff Markdown を生成する。

handoff の役割:
- tester が実行・記入する
- debugger が記入済み handoff を見てバグ発生箇所を整理する

## tester 前提と不変条件

- **tester は非エンジニア**である。CLI / DevTools / コード読解を前提にしない。
- **不変条件**: handoff と付随する setup script は、tester（非エンジニア）が端から端まで**実行・検証できる**こと。tester が実行・知覚できない手順が出たら、debugger は次のいずれかで処理する:
  - (a) 決定的な setup を**生成 setup script に寄せる**（実行コマンド1行を Scenario 0 に置く）
  - (b) 観察を**人間が知覚可能な形に変える**（例: 1px の極小 probe 画像を、setup script で視認可能サイズに生成する）
  - (c) どうしても技術操作が要る手順は **`Engineer-Required` 列**に出す（例: DevTools での CSP violation 確認）
- この処理を「誰が決めるか」はモードに従う（debugger:llm は LLM が決定、debugger:human は人間 debugger が決定し LLM は提案のみ）。

## Arguments

- `debugger`: 必須。`human` または `llm`。テスト設計（テスト要点・期待結果）を誰が所有・承認するかを選ぶ。
  - `human`: テスト設計をユーザ自身が所有・承認したいとき。人間が debugger となり、LLM は整形補助役で承認ステップあり。
  - `llm`: テスト設計を LLM に委ねるとき。LLM が debugger となり、承認なしで進める。
  - この選択はテスト対象や実行方法（実機操作の要否・GUI/CLI 等）からは推論できないユーザの作業選好である。省略時は文脈から既定を正当化せず必ず確認する。

## Mode Loading

`debugger` 引数に応じて、このスキルと同じディレクトリにあるモード別ファイルを読み込み、その指示に従え。

- `debugger:human` → `debugger_human.md` を読み込む
- `debugger:llm` → `debugger_llm.md` を読み込む

モード別ファイルの指示が Generation Flow・LLM の役割・承認フローを定義する。以下の共通仕様と合わせて適用せよ。

## Execution / Context

この skill は現セッションに inline 展開され、main session が現会話の文脈（議論 / 設計書 / source）を直接使う。

- `debugger:llm`: 生成は `subagent_type:"fork"` の subagent への1回委譲を優先する（context 隔離のため。往復・再委譲なし）。fork は会話＋inline 注入された本 skill 本文を継承し、handoff 本文のみを返す（fork の tool 出力は main に残らない）。fork が利用不能、または返却が会話既知の文脈を反映しない場合は、main が文脈を保持しているため main session で inline 生成する（正規フォールバック。error 停止しない）。いずれの経路でも、最終 handoff が会話既知の文脈を反映していることを main が確認する。
- `debugger:human`: fork へ委譲せず main session で inline 実行する（多段承認が対人逐次のため）。
- 両モードとも、保存先のユーザ確認と file write は main session が行う（fork は本文生成のみ）。main は handoff と setup script の両方を write し、setup script の絶対パスは保存先確定後に main が handoff の Scenario 0 実行コマンドへ記入する（fork は絶対パスを placeholder のままにする）。fork が確定できないセルは推測で埋めず `?` とし、不足は別途 "Unresolved" として main に返して user に提示する（書き出す handoff には 5 セクションのみ。Unresolved は永続化しない）。

## Output Structure

生成物は2つ: (1) 下記 5 セクションの handoff Markdown、(2) 決定的 setup をまとめた **setup script**（「Setup Script」節を参照）。以下 5 セクションを順に含む Markdown を生成する。これ以外の独立セクションを増やさない（5 セクション制約は handoff Markdown に対するもの。setup script は別ファイルとして出力する）。

### Section 1: Header

- 作成日時
- 対象システム名 / コンポーネント名
- 関連 path（設計書 / source / 関連ドキュメント。debugger が参照するもの）
- 引き継ぎ元 / 引き継ぎ先（任意、空欄可）

### Section 2: Pre-test Notes

コンテキストから既知問題を抽出し記載する:

- 既知の不確定事項
- 既知の放置バグ
- 想定内のズレ（tester に「これは新規バグではない」と認識させるため）
- Layer / component の vocabulary

入力にない場合は「特になし」と記す。

承認フローはモード別ファイルに従う。

### Section 3: 実行・記入ガイド

#### 実行ルール

シナリオは原則シナリオ 0 の終了状態を起点に連続実行する。各シナリオは独立に環境を初期化しない（debugger が個別指定した場合を除く）。

#### 記入ルール

Result 欄に `*` がある行は tester が `○`（Expected を満たした）または `×`（Expected と異なる結果）に置き換える。Result 欄が空欄の行は記入不要。詳細は Comments に行参照（例: `2-4:` = シナリオ 2 のステップ 4）付きで記載する。Layer 列は debugger 用、tester は読まなくてよい。

### Section 4: Test Results Table

**テーブル形式で出力せよ。リスト形式・散文形式は不可。**

#### 操作種別列

操作種別ごとに独立列にする（単一 Operation 列にするな）。該当しない行は空欄。

操作種別の例:
- CLI / shell コマンド: `Command` 列
- 自然言語 prompt: `User→LLM Message` 列
- GUI 操作: `GUI Action` 列

複数種別が混在する場合はそれぞれ独立列にし、該当しない行は空欄とせよ。

#### 基本 columns

```
| Scenario | Step | <操作種別 1> | <操作種別 2> | ... | Expected | Result |
```

Layer を含む場合:

```
| Scenario | Step | <操作種別> | ... | Expected | Layer | Result |
```

`Engineer-Required` を含む場合（tester（非エンジニア）が実行できない技術操作の手順があるとき。該当ゼロなら列ごと作らない）:

```
| Scenario | Step | <操作種別> | ... | Expected | Engineer-Required | Result |
```

#### セル記入ルール

- 行内容（操作 / Expected / Layer）は debugger が指定したものを literal で反映せよ
- pass 基準のある行は Result 欄に `*` を初期値として入れる
- pass 基準のない行は Expected 欄・Result 欄を空欄にする
- 先頭行に Scenario 0（setup）行を含める。Scenario 0 は**生成 setup script の実行コマンド1行**（絶対パスは生成時に記入）＋ script 化できない不可分な GUI 手順のみとする。断片的な setup コマンドを並べない
- `Engineer-Required` 列は、tester（非エンジニア）が実行できない技術操作（DevTools / CLI 等）の手順がある場合のみ追加し、該当行に必要手段を記入して Result は空欄にする（非エンジニアは skip しエンジニアに回す routing 列）
- `User→LLM Message` セルは貼り付け即送信できる完全 prompt（抽象・要約不可）

### Section 5: Comments

空のセクションとして用意する。LLM は事前に内容を埋めない。tester が記入時に stdout / note / 異常を行参照付きで自由記述する。

## Setup Script

handoff とは別に、決定的な setup（fixture 生成 / build・package / 同梱物の実在確認等）をまとめた setup script を1本生成する。tester は workspace で実行コマンド1つを走らせるだけでよい。

- **形式**: 実行先 workspace の環境で実行可能な形式を選ぶ（例: toolchain に node があれば `.mjs`、Windows なら PowerShell 等）。実行できない形式は不可。main session が処理系の存在を確認してから確定する
- **自己検証**: script は (1) 書き込む全 path を冒頭で宣言し、(2) 確立すべき前提（fixture の存在 / 登録行の追加等）を自分で検証し、(3) 成否を plain な文言（PASS / FAIL と理由）で出力する。エラーを握り潰さない
- **冪等**: 再実行しても壊れない（重複登録・既存 dir での失敗を避ける）。後始末（teardown）は自動で行わず、再実行可能性で担保する
- **限定**: 書き込みは対象 workspace 内に限定する
- **GUI 境界**: GUI でしか行えない操作（拡張の Reload / インストール UI 等）は script に入れず、handoff の GUI Action 行に残す。script が検証できない GUI 由来の前提（画面表示等）を「script PASS ＝ 全 setup 完了」と誤認させないよう、当該 GUI 手順を Scenario 0 に明示する
- **Scenario 0 と絶対パス**: setup script は handoff と同じディレクトリに保存し、保存後にその絶対パスを Scenario 0 の実行コマンド1行へ記入する（絶対パスの記入は生成される handoff にのみ。この SKILL.md 等の追跡対象ファイルには placeholder のみ書く）

## Output Destination

- 保存先を人間に確認してから書き込む
- slug 候補は `debug-handoff-<target-system>-<YYYY-MM-DD>.md` で自動提案
- 保存先の既定は workspace を探索して決める: `_projects/`（理想は `_projects/*/project-notes/`）が存在すれば `_projects/<project>/project-notes/checks/<slug>.md` を既定提案する（`<project>` は会話文脈で対象が明確ならそれ、曖昧なら user に質問。project routing を再実装しない）。存在しなければ neutral な保存先を slug 提案する。これは taskflow への依存ではなく、規約ディレクトリの存在検出による便乗既定である
- setup script は handoff と同じディレクトリに保存し、保存後その絶対パスを handoff の Scenario 0 実行コマンドへ記入する
- repository にディレクトリを勝手に作らない

## Rules

- input の literal を尊重し、要約・補完・整形しない
- 診断フローを生成するな
- 設計書・source の要約を handoff に入れるな（関連 path を Header に置けば十分）
- 操作列は操作種別ごとに分けよ（単一 Operation 列にするな）
- テーブル形式で出力せよ（リスト・散文・番号付き手順書は不可）
- 5 セクション（Header / Pre-test Notes / 実行・記入ガイド / Test Results / Comments）以外の独立セクションを増やすな
- debug 対象外の周辺挙動（一般 UI / 共通処理 / setup / 画面遷移など、会話コンテキストに無いもの）を推測・補完で書くな。言及が必要なら先に source を確認し、実在・実挙動を確認したもののみ記載せよ。debug 対象の Expected（あるべき挙動）はこの限りでなく spec / debugger 判断で決める
- `User→LLM Message` 列は tester がそのまま貼り付けて送信できる完全 prompt を literal で記載せよ。抽象・要約記述（例「配列型の出力を持つ」）で代替するな
- tester（非エンジニア）が実行・知覚できない手順を裸で残すな。決定的 setup は setup script に寄せ、不可視な観察は可視化し、技術操作が不可欠な手順は `Engineer-Required` 列に隔離せよ（「tester 前提と不変条件」を参照）
- setup script は決定的 setup の機械化であって診断フローではない（「診断フローを生成するな」に抵触しない）。script は作成物の path を冒頭で宣言し、確立すべき前提を自己検証して PASS / FAIL を plain な文言で出力し、冪等で、書き込みを対象 workspace に限定せよ
- 追跡対象ファイル（この SKILL.md 等）に絶対パスを literal で書くな。setup script の絶対パスは生成される handoff にのみ、main が保存先確定後に記入せよ

## Output Template

````markdown
---
title: Debug Handoff - <対象システム名>
type: handoff
created: <date>
target_system: <対象システム名>
debugger_mode: <human|llm>
handed_from: <任意>
handed_to: <任意>
---

# Debug Handoff: <対象システム名>

## Header

- Date: <date>
- Target System: <system / component>
- 関連 path: <design doc path>, <source path>, ...
- Handed From: <任意>
- Handed To: <任意>

## Pre-test Notes

<既知の不確定事項 / 放置バグ / 想定内のズレ>
<Layer / component vocabulary>

または「特になし」

## 実行・記入ガイド

### 実行ルール

シナリオは原則シナリオ 0 の終了状態を起点に連続実行する。各シナリオは独立に環境を初期化しない（debugger 個別指定時を除く）。

### 記入ルール

Result 欄に `*` がある行は `○` / `×` に置き換えて記入する。`○` は Expected を満たした、`×` は Expected と異なる結果。Result 欄が空欄の行は記入不要。詳細は Comments に行参照（例: `2-4:`）付きで記載する。Layer 列追加時は debugger 用、tester は読まなくてよい。

## Test Results

| Scenario | Step | Command | User→LLM Message | Expected | Result |
|---|---|---|---|---|---|
| 0 | 1 | `<setup script の絶対パス>` を実行 |  | script が PASS を出力（作成物・前提を確立） |  |
| 1 | 1 |  | <user prompt> | <expected> | * |
| 1 | 2 |  | <user prompt> | <expected> | * |
| 2 | 1 | <command> |  | <expected> | * |

## Comments

<tester 記入欄、空で生成>
````
