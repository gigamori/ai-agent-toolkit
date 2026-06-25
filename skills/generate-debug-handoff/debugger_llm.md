# debugger:llm モード

## LLM の役割

LLM が debugger 本体を務める。テスト要点・期待結果は LLM がコンテキストから決定し、承認なしで進める。

## Generation Flow

このモードは生成を fork subagent への **1 回委譲を優先**する（context 隔離のため。往復・再委譲なし）。fork が利用不能・劣化時は main inline 生成にフォールバックする（main が会話文脈を保持するため安全）。

### main（委譲前）

1. fork directive を構築する:「継承文脈に inline 注入済の本 skill の Output Template / Setup Script / Rules に従い debugger:llm で handoff 本文と setup script を 1 パス生成せよ。tester（非エンジニア）が実行・知覚できない手順は (a) setup script 化 / (b) 可視化 / (c) `Engineer-Required` 列 で処理せよ。文脈で確定できないセルは推測で埋めず `?` とし、不足項目は handoff 本文とは別に "Unresolved" として列挙して返せ（handoff の独立セクションにはしない）。handoff Markdown と setup script 本文を返せ（write 禁止。setup script の絶対パスは未確定なので placeholder のままにせよ）」。canary 値（会話既知の target 等）は directive に書かない
2. `subagent_type:"fork"` で 1 回 spawn する（再委譲しない）

### fork（1 パス生成）

1. debug 対象外・コンテキスト外の周辺挙動に言及が要る場合、推測せず先に source を確認して裏取りする
2. 会話コンテキスト（議論 / 設計書 / source）からテストシナリオ・操作内容・期待結果を決定する
3. Pre-test Notes（既知問題・放置バグ・想定内のズレ）を抽出する。Layer / component vocabulary を決定する（必要な場合のみ）
4. 確定できないセルは推測で埋めず `?` とする。不足項目は本文と別に "Unresolved" として列挙する
5. SKILL.md の Output Template / Setup Script 節に従い handoff 本文と setup script を生成し、Markdown と script 本文を返す（write しない。setup script の絶対パスは placeholder のまま）

### main（受領後）

6. 採用判定: fork 返却が会話既知の値（target / シナリオ / 期待結果等）を反映し正規テーブル形式なら、それを採用する。fork が利用不能、または返却が文脈を反映しない / 空 / 非テーブルの場合は、main が会話文脈を用いて inline 生成する（正規フォールバック。error 停止しない）
7. "Unresolved" があれば user に提示する（往復ループはしない。user は不足を補って再 invoke できる）。書き出す handoff は 5 セクションのみで、Unresolved は file に含めない
8. 保存先を user に確認する（slug 候補を自動提案。Output Destination の convention 検出に従う）
9. setup script の形式が実行先 env で実行可能か（処理系の存在）を確認する。handoff と setup script を同じディレクトリに書き出し、setup script の絶対パスを handoff の Scenario 0 実行コマンドへ記入する

## 質問ポリシー

コンテキストから判断できる情報は fork が決定する。不足情報は推測で埋めず `?` / "Unresolved" として返し、main が user に提示する（生成中の対話往復はしない）。

## テーブル生成ルール

- コンテキストからテストシナリオ・操作内容・期待結果を決定し、テーブルに反映する
- 操作種別列の構成はコンテキストの操作種別に応じて LLM が決定する
- Layer 列はコンテキストに layer / component 情報がある場合に LLM が追加する
- `Engineer-Required` 列は、tester（非エンジニア）が実行できない技術操作（DevTools / CLI 等）の手順がある場合に LLM が追加し、該当行に必要手段を記し Result は空欄にする（非エンジニアは skip しエンジニアに回す）
- 出力は必ず SKILL.md の Output Template のテーブル形式に従う
