---
name: generate-debug-handoff
description: Generate a debug handoff Markdown for E2E testing. The debugger argument (human/llm) is required. With debugger:human the LLM only formats and the human approves; with debugger:llm the LLM acts as the debugger with no approval. Use when "debug handoff", "test handoff", or "E2E test table" is mentioned.
---

# Debug Handoff Generation

## Overview

Generate a debug handoff Markdown for isolating defects in E2E testing.

Roles of the handoff:
- The tester runs it and fills it in.
- The debugger reads the filled-in handoff and pinpoints where the bug occurs.

## Tester premise and invariants

- **The tester is a non-engineer.** Do not assume CLI / DevTools / code reading.
- **Invariant**: the handoff and its accompanying setup script must be **executable and verifiable end to end by the tester (a non-engineer)**. If a step appears that the tester cannot run or perceive, the debugger handles it in one of the following ways:
  - (a) **Push deterministic setup into the generated setup script** (place the single run command in Scenario 0).
  - (b) **Turn the observation into something a human can perceive** (e.g. generate a 1px probe image at a visible size via the setup script).
  - (c) Steps that truly require technical operation go into the **`Engineer-Required` column** (e.g. confirming a CSP violation in DevTools).
- "Who decides" this handling follows the mode (with debugger:llm the LLM decides; with debugger:human the human debugger decides and the LLM only proposes).

## Arguments

- `debugger`: required. `human` or `llm`. Selects who owns and approves the test design (test points and expected results).
  - `human`: the user owns/approves the test design. The human is the debugger; the LLM only formats, and an approval step exists.
  - `llm`: the test design is delegated to the LLM. The LLM is the debugger and proceeds without approval.
  - Before anything else, check whether the invocation literally contains `debugger:human` or `debugger:llm`, and quote it. If you cannot quote either from the invocation, do not pick or infer a default — ask the user which, then proceed. This is a work preference (who approves), not something derivable from the target or the execution method.
  - Known failure to avoid: the conversation will be saturated with talk of real-device / GUI / CLI / "a person operates the machine," because that is the test material — it is not evidence for the debugger preference. If you catch yourself reasoning from how the test is executed toward `human` or `llm`, that is the failure; stop and ask instead.

## Mode Loading

Depending on the `debugger` argument, load the mode-specific file in the same directory as this skill and follow its instructions.

- `debugger:human` → load `debugger_human.md`
- `debugger:llm` → load `debugger_llm.md`

The mode-specific file defines the Generation Flow, the LLM's role, and the approval flow. Apply it together with the common specification below.

## Execution / Context

This skill is expanded inline in the current session; the main session uses the current conversation's context (discussion / design docs / source) directly.

- `debugger:llm`: prefer a single delegation of generation to a `subagent_type:"fork"` subagent (for context isolation; no round trips, no re-delegation). The fork inherits the conversation plus this skill body injected inline, and returns only the handoff body (the fork's tool output does not remain in main). If the fork is unavailable, or its return does not reflect context known from the conversation, the main session generates inline because main holds the context (this is the canonical fallback; do not stop with an error). On either path, main confirms that the final handoff reflects context known from the conversation.
- `debugger:human`: do not delegate to a fork; run inline in the main session (because multi-step approval is interactive and sequential).
- In both modes, the user confirmation of the destination and the file write are done by the main session (the fork only generates the body). Main writes both the handoff and the setup script, and after the destination is fixed, main fills the setup script's absolute path into the Scenario 0 run command of the handoff (the fork leaves the absolute path as a placeholder). For any cell the fork cannot determine, do not fill it by guessing — use `?` and return shortfalls separately as "Unresolved" for main to present to the user (the written handoff contains only the 5 sections; Unresolved is not persisted).

## Output Structure

There are two artifacts: (1) the handoff Markdown with the 5 sections below, and (2) a **setup script** that bundles deterministic setup (see the "Setup Script" section). Generate Markdown containing the following 5 sections in order. Do not add any independent section beyond these (the 5-section constraint applies to the handoff Markdown; the setup script is output as a separate file).

### Section 1: Header

- Created date/time
- Target system name / component name
- Related paths (design docs / source / related documents the debugger refers to)
- Handed from / handed to (optional, may be blank)

### Section 2: Pre-test Notes

Extract known issues from the context and record them:

- Known uncertainties
- Known unfixed bugs
- Expected deviations (so the tester recognizes "this is not a new bug")
- Layer / component vocabulary

If none in the input, write "None".

The approval flow follows the mode-specific file.

### Section 3: Run/Fill Guide

#### Run rules

In principle the scenarios run consecutively starting from the end state of Scenario 0. Each scenario does not initialize the environment independently (unless the debugger specifies otherwise).

#### Fill rules

For rows whose Result cell has `*`, the tester replaces it with `○` (met the Expected) or `×` (differed from Expected). Rows with an empty Result cell need no entry. Record details in Comments with a row reference (e.g. `2-4:` = scenario 2, step 4). The Layer column is for the debugger; the tester need not read it.

### Section 4: Test Results Table

**Output in table form. List form or prose form is not allowed.**

#### Operation-type columns

Make each operation type its own column (do not use a single Operation column). Leave non-applicable rows blank.

Examples of operation types:
- CLI / shell command: `Command` column
- Natural-language prompt: `User→LLM Message` column
- GUI operation: `GUI Action` column

When multiple types are mixed, make each its own column and leave non-applicable rows blank.

#### Base columns

```
| Scenario | Step | <operation type 1> | <operation type 2> | ... | Expected | Result |
```

When a Layer is included:

```
| Scenario | Step | <operation type> | ... | Expected | Layer | Result |
```

When `Engineer-Required` is included (when there are steps the non-engineer tester cannot run; if there are zero such rows, do not create the column at all):

```
| Scenario | Step | <operation type> | ... | Expected | Engineer-Required | Result |
```

#### Cell-fill rules

- Reflect row content (operation / Expected / Layer) literally as the debugger specified
- For rows with a pass criterion, put `*` as the initial value in the Result cell
- For rows without a pass criterion, leave the Expected and Result cells blank
- Include a Scenario 0 (setup) row as the first row. Scenario 0 is **the single run command of the generated setup script** (absolute path filled at generation time) plus any indivisible GUI steps that cannot be scripted. Do not list fragmentary setup commands
- Add the `Engineer-Required` column only when there are steps the non-engineer tester cannot run (DevTools / CLI, etc.); fill the needed means in the relevant row and leave Result blank (a routing column where the non-engineer skips and hands off to an engineer)
- `User→LLM Message` cells must be complete prompts that can be pasted and sent as-is (no abstraction or summarization)

### Section 5: Comments

Provide it as an empty section. The LLM does not fill in content in advance. The tester writes freely at fill time with row references for stdout / notes / anomalies.

## Setup Script

Separately from the handoff, generate one setup script that bundles the deterministic setup (fixture generation / build·package / presence checks of bundled items, etc.). The tester only runs a single run command in the workspace.

- **Format**: choose a format runnable in the target workspace's environment (e.g. `.mjs` if the toolchain has node, PowerShell on Windows, etc.). A non-runnable format is not allowed. The main session confirms the runtime exists before fixing it
- **Self-verification**: the script (1) declares all paths it writes at the top, (2) verifies by itself the premises to establish (existence of fixtures / addition of registration lines, etc.), and (3) outputs success/failure in plain wording (PASS / FAIL with reason). Do not swallow errors
- **Idempotent**: re-running does not break it (avoid duplicate registration / failure on existing dirs). Do not perform teardown automatically; guarantee via re-runnability
- **Bounded**: writes are limited to within the target workspace
- **GUI boundary**: operations possible only via GUI (extension Reload / install UI, etc.) are not put in the script; leave them in the handoff's GUI Action rows. To avoid misreading "script PASS = all setup done" for GUI-derived premises the script cannot verify (screen display, etc.), state those GUI steps explicitly in Scenario 0
- **Scenario 0 and absolute path**: save the setup script in the same directory as the handoff, and after saving, fill its absolute path into the single run command of Scenario 0 (the absolute path is filled only into the generated handoff; in this SKILL.md and other tracked files, write only a placeholder)

## Output Destination

- Confirm the destination with the human before writing
- Auto-suggest a slug candidate as `debug-handoff-<target-system>-<YYYY-MM-DD>.md`
- Default destination is decided by exploring the workspace: if `_projects/` exists (ideally `_projects/*/project-notes/`), suggest `_projects/<project>/project-notes/checks/<slug>.md` as the default (`<project>` is the one clear from the conversation context if the target is clear, otherwise ask the user; do not re-implement project routing). If it does not exist, suggest a neutral destination slug. This is not a dependency on taskflow but an opportunistic default based on detecting a conventional directory
- Save the setup script in the same directory as the handoff, and after saving fill its absolute path into the Scenario 0 run command of the handoff
- Do not create directories in the repository arbitrarily

## Rules

- Respect the literal of the input; do not summarize, complete, or reformat
- Do not generate a diagnostic flow
- Do not put summaries of design docs / source into the handoff (placing related paths in the Header is enough)
- Split operation columns by operation type (do not use a single Operation column)
- Output in table form (list / prose / numbered procedure is not allowed)
- Do not add any independent section beyond the 5 (Header / Pre-test Notes / Run-Fill Guide / Test Results / Comments)
- Do not write, by guessing or completion, peripheral behavior outside the debug target (general UI / common processing / setup / screen transitions, etc. that are not in the conversation context). If mention is needed, confirm the source first and record only what is confirmed to exist and behave so. The Expected (intended behavior) of the debug target is exempt from this and is decided by spec / debugger judgment
- Verify each scenario row on its own before output:
  - `Command` cell: actually run the command once and write only the observed exit code / output
  - `Expected` cell: open the real source of that branch and confirm the condition truly holds under the fixture / config you prepared. If it does not hold, do not write the row (`debugger:human`: report it to the debugger instead of writing it)
  - Premise wording (order / mechanism) in Run rules and Pre-test Notes: write it from the actual source lines, not from memory or design docs
- The `User→LLM Message` column must record complete prompts the tester can paste and send as-is. Do not substitute with abstract / summarized descriptions (e.g. "has an array-typed output")
- Do not leave bare steps the non-engineer tester cannot run or perceive. Push deterministic setup into the setup script, make invisible observations visible, and isolate steps that unavoidably require technical operation into the `Engineer-Required` column (see "Tester premise and invariants")
- The setup script is the mechanization of deterministic setup, not a diagnostic flow (it does not conflict with "do not generate a diagnostic flow"). The script declares the paths of its artifacts at the top, self-verifies the premises to establish, outputs PASS / FAIL in plain wording, is idempotent, and limits writes to the target workspace
- Do not write absolute paths literally into tracked files (this SKILL.md, etc.). The setup script's absolute path is filled only into the generated handoff, by main after the destination is fixed

## Output Template

````markdown
---
title: Debug Handoff - <target system name>
type: handoff
created: <date>
target_system: <target system name>
debugger_mode: <human|llm>
handed_from: <optional>
handed_to: <optional>
---

# Debug Handoff: <target system name>

## Header

- Date: <date>
- Target System: <system / component>
- Related paths: <design doc path>, <source path>, ...
- Handed From: <optional>
- Handed To: <optional>

## Pre-test Notes

<known uncertainties / unfixed bugs / expected deviations>
<Layer / component vocabulary>

or "None"

## Run/Fill Guide

### Run rules

In principle the scenarios run consecutively starting from the end state of Scenario 0. Each scenario does not initialize the environment independently (except when the debugger specifies otherwise).

### Fill rules

For rows whose Result cell has `*`, replace it with `○` / `×`. `○` means it met the Expected, `×` means it differed from the Expected. Rows with an empty Result cell need no entry. Record details in Comments with a row reference (e.g. `2-4:`). When a Layer column is added it is for the debugger; the tester need not read it.

## Test Results

| Scenario | Step | Command | User→LLM Message | Expected | Result |
|---|---|---|---|---|---|
| 0 | 1 | Run `<absolute path of the setup script>` |  | The script outputs PASS (establishes artifacts/premises) |  |
| 1 | 1 |  | <user prompt> | <expected> | * |
| 1 | 2 |  | <user prompt> | <expected> | * |
| 2 | 1 | <command> |  | <expected> | * |

## Comments

<tester fill-in area, generated empty>
````
