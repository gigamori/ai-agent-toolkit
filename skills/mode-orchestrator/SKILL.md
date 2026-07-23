---
name: mode-orchestrator
description: Read a document that holds a todolist (a list of instructions) plus related context, then run each step as an isolated general-purpose subagent turn prefixed with a role-mode mode:/role: header and the matching NEVER/DO rules — one mode and at most one role per turn, never mixed, autonomous modes only. First gates the input and rejects an insufficient todolist. Use when the user points at a design doc, plan, or handoff that contains a todolist and asks to orchestrate, run, or execute its steps mode-by-mode, or mentions role-mode driven subagent execution.
---

# Mode Orchestrator

## Overview

Takes a document that contains a todolist (a list of instructions) and related context. For each step it generates a role-mode-tagged prompt (picks the mode, optionally a role) and runs it as a separate, isolated subagent turn. One mode — and at most one role — per turn; modes and roles are never mixed within a single turn. Only autonomous modes are executed; interactive modes are surfaced as suggestions, not run.

This skill does not decompose an unstructured task from scratch — the todolist must already be present in the input (see Step 0).

## Input

- A document (any format — the format is not fixed) whose content includes:
  - a **todolist**: a list of instructions/steps to carry out, and
  - the **related context** those steps need.
- The whole document is read to extract the todolist and context. An optional scope hint (e.g., a section) may narrow which steps to run.
- Pass the document by path. Examples here use placeholders such as `path/to/design.md`.

## Flags

- `--auto`: skip the approval gate and run all turns without per-plan confirmation. Default: present the turn plan and wait for approval.
- `--roles` or `--roles=always`: infer and attach a fitting role to every turn. Default (`none`): do not infer roles, but honor any role explicitly stated in the todolist.

Flags use the `--` form on purpose. Never use `mode:` / `role:` colon-prefixes for flags — the role-mode hook would capture them from the invocation prompt.

## Step 0 — Todolist sufficiency gate (reject)

Run this first, before any generation or execution. REJECT and stop (generate and execute nothing) if any of these holds:

- No identifiable list of instructions/steps is present.
- Steps are too vague to act on or to map to a mode (not actionable).
- The context required to carry out the steps is missing.

On reject: report exactly what is missing or insufficient — name the offending steps and the absent context — and ask the user to supply a sufficient todolist. Do not guess or fill gaps to proceed.

## Mode catalog and routing

Hardcoded mode list (rules bundled under `modes/`). Aliases: `verify` → `debug`, `implement` → `execute`.

Autonomous — executed as a subagent turn:

- `survey`, `plan`, `execute`, `debug`, `review`, `review-dev`

Interactive — NOT executed; surfaced as a suggestion to run natively with role-mode:

- `ask`, `discuss`, `brainstorm`, `organize`

If a step resolves to an interactive mode, do not run it; note it for the user to handle natively (interactive modes need a live human exchange, which an autonomous subagent cannot provide).

The mode rules are bundled in this skill's `modes/` directory: `_meta.md`, `_common.md`, and one `<mode>.md` for each of the 6 autonomous modes above. Read them from there — do not improvise the rules. The interactive modes are not bundled, since they are never executed.

## Mode and role decision (the generation step)

For each step:

- **Mode — hybrid**: if the step explicitly names a mode, honor it; otherwise infer the fitting mode from the step's content.
- **Role — hybrid**: if the step explicitly states a role, honor it; otherwise follow the `--roles` policy (default `none`: no role; `always`: infer one).

## Turn plan

Build an ordered list of turn records. Each record:

- `order`, `mode`, `role` (optional), `inputs` (file paths), `instruction`

One mode per record — a record never carries two modes or two roles. A single section or step may expand into multiple records when it needs different modes; split at every mode change. This record shape is what structurally guarantees no mixing.

Unless `--auto`, present the turn plan (order / mode / role / one-line gist per turn) and wait for approval before executing.

## Injection assembly (per subagent prompt)

Embed the role-mode rules into each subagent prompt. Read the bundled files from `modes/` and assemble the prompt prefix in this exact order:

With a role:

1. `_meta.md` (verbatim)
2. `role: <value>` (one line)
3. `mode: <name>` (one line)
4. `<mode>.md` (verbatim)
5. `_common.md` (verbatim)

Without a role: the same, omitting line 2.

Then append the step's instruction, the inputs as file paths (not inlined content), and a deliverable-write clarification: writing the single deliverable file is this mode's own output document (per its DO — e.g., `create-process-documents` / `create-design-documents`, report findings, `report-completion`); the mode's `NO write/edit` is an OVERRIDE-clause constraint on editing target/source code under a fix/implement/edit demand, and does not forbid authoring this deliverable. (`execute`: editing target source is the task; the deliverable is a change-report.)

Worked example — a `plan` turn with a role, fully assembled:

```
<contents of _meta.md>

role: senior migration engineer
mode: plan
<contents of plan.md>
<contents of _common.md>

Task: <the step's instruction, verbatim>
Context to read: path/to/design.md, run/01-survey.md
Write your deliverable to: run/02-plan.md
Note: writing this deliverable is your mode's own design document (plan DO: create-design-documents); NO write/edit applies to editing target/source code under an implement demand, not to this file.
Return only: status (ok|blocked|needs-human), the output file path, and a 3-line gist.
```

## Execution

For each turn record, in order:

1. Assemble the subagent prompt (above).
2. Delegate to one general-purpose subagent. One turn = one subagent; never combine turns.
3. The subagent writes its deliverable to the run directory as `NN-<mode>.md` and returns only: status (`ok` | `blocked` | `needs-human`), the file path, and a ≤3-line gist.
   - **execute exception**: an `execute` turn edits the actual source files; its file is a short change-report listing the touched paths, not a copy of the work.
   - If the subagent reports `[BLOCKED: mode-rule <name>]`, relay it verbatim.
4. **Chaining**: a later turn receives earlier artifacts by path in its `inputs` and reads the full files itself — never forward a gist as the next turn's input.
5. On status `blocked` or `needs-human`: stop the run and report verbatim (a dependent step cannot run without its input). Record progress in the run index.
6. After all turns: summarize the run directory's artifacts and gists.

## Run directory (workspace)

- Create one run directory in the workspace per invocation, e.g. `mode-orchestrator-runs/<run-slug>/` — derive the slug from the input document name.
- Artifacts: `NN-<mode>.md` in order, plus a small index file recording the turn plan and each turn's status/path. This index is an artifact index for inspection, not a resumable scheduler.
- These are runtime artifacts; do not commit them.

## Context discipline

- This skill reads the whole input document to generate the turn plan.
- Downstream, subagents receive their inputs as **file paths only** — never inline the document's raw content into a subagent prompt. This keeps each subagent's context clean and isolated.
- Prefer invoking the skill in a fresh session. The input document is bounded structured content, but a clean session keeps the orchestration context lean.

## Out of scope

- No rollback / checkpoint / worktree around `execute` turns — working-tree safety is the user's git hygiene (and CLAUDE.md) concern, not this skill's.
- No mid-run interactive handoff and no resume scheduler.
- No helper scripts — prompt assembly and the Step 0 gate are done in-prompt.
- No zero-decomposition of an unstructured task — the todolist must be in the input.

## Known residual risks

- Over a long run the orchestration context grows; this is mitigated by gist-only returns, short pipelines, and passing inputs by path.
