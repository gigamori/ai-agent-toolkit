# debugger:human モード

## 実行コンテキスト

このモードは fork へ委譲せず main session で inline 実行する（多段承認が対人逐次のため）。承認フローは以下変更なし。

## LLM の役割

LLM は debugger（人間）の指示を Markdown テーブルに整形する補助役である。テスト要点・期待結果の最終決定は debugger（人間）が行う。LLM が独断で決めるな。

## Generation Flow

1. 会話コンテキスト（設計書 / source / 議論）を確認する
2. debug 対象外・コンテキスト外の周辺挙動に言及する必要が出たら、推測せず先に source を確認して裏取りし、debugger に提示せよ。確認できないものは debugger に質問せよ
3. 不明な情報は推測で穴埋めせず、**必ず** debugger に質問して引き出す
4. コンテキストから Pre-test Notes（既知問題・放置バグ・想定内のズレ）を抽出し提示する
5. debugger が Pre-test Notes を確認し、追加・削除・修正を指示する → **承認を得てから次に進む**
6. Layer / component vocabulary をコンテキストから提案する（debugger が追加・削除を指示できる）→ **承認を得てから次に進む**
7. debugger の指示に基づき、SKILL.md の Output Template に従って Test Results テーブルを整形する
8. 完成したドラフトを debugger に提示する → **承認を得てから書き出す**
9. 保存先を debugger に確認する（slug 候補を自動提案）
10. handoff を書き出す

## 質問ポリシー

不明な情報は推測で穴埋めせず、必ず debugger に質問せよ。debugger は必要に応じて coder から情報を引き出すが、handoff の意思決定者は debugger 単独である。

## テーブル生成ルール

- debugger が指定したテストシナリオ・操作内容・期待結果を literal でテーブルに反映する
- LLM が独自にシナリオを追加・変更・省略するな
- 操作種別列の構成は debugger の指示に従う
- Layer 列は debugger が指定した場合のみ追加する
