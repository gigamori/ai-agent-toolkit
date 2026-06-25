# debugger:llm mode

## The LLM's role

The LLM serves as the debugger itself. The test points and expected results are decided by the LLM from the context, and it proceeds without approval.

## Generation Flow

This mode prefers a **single delegation** of generation to a fork subagent (for context isolation; no round trips, no re-delegation). When the fork is unavailable or degraded, fall back to inline generation in main (safe because main holds the conversation context).

### main (before delegation)

1. Build the fork directive: "Following the Output Template / Setup Script / Rules of this skill injected inline into the inherited context, generate the handoff body and the setup script in one pass as debugger:llm. For steps the non-engineer tester cannot run or perceive, handle them via (a) the setup script / (b) visualization / (c) the `Engineer-Required` column. For cells you cannot determine from the context, do not fill by guessing — use `?`, and list shortfalls separately from the handoff body as 'Unresolved' (do not make them an independent section of the handoff). Return the handoff Markdown and the setup script body (writing is forbidden; leave the setup script's absolute path as a placeholder because it is not yet fixed)." Do not write canary values (such as a target known from the conversation) into the directive
2. Spawn once with `subagent_type:"fork"` (no re-delegation)

### fork (single-pass generation)

1. If you need to mention peripheral behavior outside the debug target or outside the context, do not guess — confirm the source first
2. From the conversation context (discussion / design docs / source), decide the test scenarios, operation content, and expected results
3. Extract Pre-test Notes (known issues / unfixed bugs / expected deviations). Decide the Layer / component vocabulary (only when needed)
4. For cells you cannot determine, do not fill by guessing — use `?`. List shortfalls separately from the body as "Unresolved"
5. Following the Output Template / Setup Script sections of SKILL.md, generate the handoff body and the setup script, and return the Markdown and the script body (do not write; leave the setup script's absolute path as a placeholder)

### main (after receipt)

6. Adoption check: if the fork return reflects values known from the conversation (target / scenarios / expected results, etc.) and is in proper table form, adopt it. If the fork is unavailable, or the return does not reflect the context / is empty / is not a table, main generates inline using the conversation context (the canonical fallback; do not stop with an error)
7. If there is "Unresolved", present it to the user (no round-trip loop; the user can fill the gaps and re-invoke). The written handoff has only the 5 sections; Unresolved is not included in the file
8. Confirm the destination with the user (auto-suggest a slug candidate; follow the convention detection in Output Destination)
9. Confirm whether the setup script's format is runnable in the target environment (the runtime exists). Write the handoff and the setup script to the same directory, and fill the setup script's absolute path into the Scenario 0 run command of the handoff

## Question policy

Information that can be judged from the context is decided by the fork. Do not fill missing information by guessing — return it as `?` / "Unresolved" and main presents it to the user (no interactive round trips during generation).

## Table generation rules

- Decide test scenarios, operation content, and expected results from the context and reflect them in the table
- The LLM decides the composition of operation-type columns according to the operation types in the context
- The LLM adds a Layer column when the context has layer / component information
- The LLM adds the `Engineer-Required` column when there are steps the non-engineer tester cannot run (DevTools / CLI, etc.); fill the needed means in the relevant row and leave Result blank (the non-engineer skips and hands off to an engineer)
- The output must follow the table form of the Output Template in SKILL.md
