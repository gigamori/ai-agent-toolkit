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

## Arguments

- `debugger`: 必須。`human` または `llm`。
  - `human`: 人間が debugger。LLM は整形補助役。承認ステップあり。
  - `llm`: LLM が debugger。承認なしで進める。
  - 省略時は確認を求める。

## Mode Loading

`debugger` 引数に応じて、このスキルと同じディレクトリにあるモード別ファイルを読み込み、その指示に従え。

- `debugger:human` → `debugger_human.md` を読み込む
- `debugger:llm` → `debugger_llm.md` を読み込む

モード別ファイルの指示が Generation Flow・LLM の役割・承認フローを定義する。以下の共通仕様と合わせて適用せよ。

## Execution / Context

この skill は現セッションに inline 展開され、main session が現会話の文脈（議論 / 設計書 / source）を直接使う。

- `debugger:llm`: 生成は `subagent_type:"fork"` の subagent への1回委譲を優先する（context 隔離のため。往復・再委譲なし）。fork は会話＋inline 注入された本 skill 本文を継承し、handoff 本文のみを返す（fork の tool 出力は main に残らない）。fork が利用不能、または返却が会話既知の文脈を反映しない場合は、main が文脈を保持しているため main session で inline 生成する（正規フォールバック。error 停止しない）。いずれの経路でも、最終 handoff が会話既知の文脈を反映していることを main が確認する。
- `debugger:human`: fork へ委譲せず main session で inline 実行する（多段承認が対人逐次のため）。
- 両モードとも、保存先のユーザ確認と file write は main session が行う（fork は本文生成のみ）。fork が確定できないセルは推測で埋めず `?` とし、不足は別途 "Unresolved" として main に返して user に提示する（書き出す handoff には 5 セクションのみ。Unresolved は永続化しない）。

## Output Structure

以下 5 セクションを順に含む Markdown を生成する。これ以外の独立セクションを増やさない。

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

#### セル記入ルール

- 行内容（操作 / Expected / Layer）は debugger が指定したものを literal で反映せよ
- pass 基準のある行は Result 欄に `*` を初期値として入れる
- pass 基準のない行は Expected 欄・Result 欄を空欄にする
- 先頭行に Scenario 0（setup）等の特殊行を含める
- `User→LLM Message` セルは貼り付け即送信できる完全 prompt（抽象・要約不可）

### Section 5: Comments

空のセクションとして用意する。LLM は事前に内容を埋めない。tester が記入時に stdout / note / 異常を行参照付きで自由記述する。

## Output Destination

- 保存先を人間に確認してから書き込む
- slug 候補は `debug-handoff-<target-system>-<YYYY-MM-DD>.md` で自動提案
- `_projects/` 配下には書かない
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
| 0 | 1 | <setup command> |  |  |  |
| 1 | 1 |  | <user prompt> | <expected> | * |
| 1 | 2 |  | <user prompt> | <expected> | * |
| 2 | 1 | <command> |  | <expected> | * |

## Comments

<tester 記入欄、空で生成>
````
