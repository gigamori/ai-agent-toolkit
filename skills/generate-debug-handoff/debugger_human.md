# debugger:human mode

## Execution context

This mode does not delegate to a fork; it runs inline in the main session (because multi-step approval is interactive and sequential). The approval flow below is unchanged.

## The LLM's role

The LLM is an assistant that formats the debugger's (human's) instructions into a Markdown table. The final decision on test points and expected results is made by the debugger (human). The LLM must not decide on its own.

## Generation Flow

1. Review the conversation context (design docs / source / discussion)
2. When you need to mention peripheral behavior outside the debug target or outside the context, do not guess — confirm the source first and present it to the debugger. Ask the debugger about anything you cannot confirm
3. Do not fill unknown information by guessing; **always** ask the debugger to draw it out
4. Extract Pre-test Notes (known issues / unfixed bugs / expected deviations) from the context and present them
5. The debugger reviews the Pre-test Notes and instructs additions / deletions / corrections → **get approval before proceeding**
6. Propose the Layer / component vocabulary from the context (the debugger can instruct additions / deletions) → **get approval before proceeding**
7. Based on the debugger's instructions, format the Test Results table following the Output Template of SKILL.md. For steps the non-engineer tester cannot run or perceive, propose to the debugger one of **(a) the setup script / (b) visualization / (c) the `Engineer-Required` column** and get approval (the LLM does not decide on its own). Draft deterministic setup as a setup script following the Setup Script section of SKILL.md
8. Present the completed draft (handoff + setup script) to the debugger → **get approval before writing**
9. Confirm the destination with the debugger (auto-suggest a slug candidate; follow the convention detection in Output Destination)
10. Write the handoff and the setup script to the same directory, and fill the setup script's absolute path into the Scenario 0 run command of the handoff

## Question policy

Do not fill unknown information by guessing; always ask the debugger. The debugger draws out information from the coder as needed, but the sole decision-maker for the handoff is the debugger.

## Table generation rules

- Reflect the test scenarios, operation content, and expected results the debugger specified into the table literally
- Do not add, change, or omit scenarios on your own
- The composition of operation-type columns follows the debugger's instructions
- Add a Layer column only when the debugger specifies it
- Add the `Engineer-Required` column when there are steps that unavoidably require technical operation (propose the need to the debugger and seek their judgment). Leave the Result blank for the relevant rows
