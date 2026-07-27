---
name: mode-orchestrator
description: "Read a document that holds a todolist (a list of instructions) plus related context, then run each step as an isolated general-purpose subagent turn prefixed with a role-mode mode:/role: header and the matching NEVER/DO rules — one mode and at most one role per turn, never mixed, autonomous modes only. Supports a per-turn model override, a bounded failed→debug→re-execute recovery loop, and optional per-task-type workflow specs that supply default step sequences and mode→model tables. First gates the input and rejects an insufficient todolist. Use when the user points at a design doc, plan, or handoff that contains a todolist and asks to orchestrate, run, or execute its steps mode-by-mode, or mentions role-mode driven subagent execution."
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
- `--workflow=<name>`: load `workflows/<name>.md` and apply it as defaults (recommended step sequence, mode→model table, failure-policy parameters). A spec name declared inside the todolist document is honored the same way. Default: no spec — run exactly as the todolist dictates (backward compatible). A spec supplies defaults and warnings only; the todolist is always authoritative and the Step 0 gate is unchanged.

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

## Mode, role, and model decision (the generation step)

For each step:

- **Mode — hybrid**: if the step explicitly names a mode, honor it; otherwise infer the fitting mode from the step's content.
- **Role — hybrid**: if the step explicitly states a role, honor it; otherwise follow the `--roles` policy (default `none`: no role; `always`: infer one).
- **Model — precedence, no inference**:
  1. **Per-step explicit model** — named on the todolist step, or pinned for that step in the active spec's recommended sequence; the todolist wins on conflict — honor it.
  2. **Spec mode→model table** — the active spec's blanket default for this mode.
  3. **Inherit** — no override; the session model is used.

  Never guess a model from the mode alone. Record which tier decided (step / table / inherit) so the turn plan can show it.

## Turn plan

Build an ordered list of turn records. Each record:

- `order`, `mode`, `role` (optional), `model` (optional), `inputs` (file paths), `instruction`

One mode per record — a record never carries two modes or two roles. A single section or step may expand into multiple records when it needs different modes; split at every mode change. This record shape is what structurally guarantees no mixing.

If a workflow spec is active, compare the todolist against the spec's recommended step sequence; note any mismatch (a step the spec does not anticipate, or a spec step the todolist omits) as a **warning** appended to the turn plan. A warning never blocks — the todolist is authoritative; only the Step 0 gate can reject.

Include a **Failure policy** block in the turn plan, stated once up front: which turn kinds can enter recovery (execute turns that return `failed` — no other mode is even offered that status), the per-turn cycle cap (default 2, or the active spec's value), the exit rule (cap reached → escalate to `blocked` and stop), the wall-clock budget each turn is given (the watchdog's per-mode deadline, and its stall threshold); and everything that bypasses recovery — `blocked` / `needs-human` stop immediately; an `aborted` turn — one with no status line, or one the watchdog ended with `TIMEOUT` or `STALL` — is re-run once outside the cap, then stops with `needs-human`; and `failed` from a non-`execute` turn is out of contract and stops as `needs-human`. Approving the plan approves this policy; the recovery turns it later inserts are not re-approved individually (this holds even without `--auto`).

Unless `--auto`, present the turn plan (order / mode / role / model + its decided tier / one-line gist per turn), the spec warnings, and the Failure policy block, then wait for approval before executing.

## Injection assembly (per subagent prompt)

Embed the role-mode rules into each subagent prompt. Read the bundled files from `modes/` and assemble the prompt prefix in this exact order:

With a role:

1. `_meta.md` (verbatim)
2. `role: <value>` (one line)
3. `mode: <name>` (one line)
4. `<mode>.md` (verbatim)
5. `_common.md` (verbatim)

Without a role: the same, omitting line 2.

The resolved `model` is **not** part of the prompt text — it is passed as the model override on the delegation call itself (see Execution). Do not write the model name into the assembled prompt.

Then append the step's instruction, the inputs as file paths (not inlined content), and a deliverable-write clarification: writing the single deliverable file is this mode's own output document (per its DO — e.g., `create-process-documents` / `create-design-documents`, report findings, `report-completion`); the mode's `NO write/edit` is an OVERRIDE-clause constraint on editing target/source code under a fix/implement/edit demand, and does not forbid authoring this deliverable. (`execute`: editing target source is the task; the deliverable is a change-report. `debug`: the deliverable is a root-cause report plus a proposed minimal diff and a verification command; debug never applies the diff itself.)

Finally, append the **reply contract** verbatim — it is what makes a turn's outcome readable, and a turn without it cannot be classified.

**The status vocabulary depends on the turn's mode.** `failed` means "the procedure ran and a planned check did not pass", and only an `execute` turn runs such a check — so only an `execute` turn is offered that value. Every other mode gets the three-value list. Offering `failed` to a mode that can never legitimately produce it invites exactly the misclassification this contract exists to prevent.

For an `execute` turn:

```
Reply contract — the FINAL line of your reply must be exactly:
status: <ok|failed|blocked|needs-human>; file: <path>
Use `file: -` when this turn produces no file. Put your <=3-line gist
above that line. If any tool call of yours was denied by the permission
system, the status is `blocked` — never `failed`.
```

For every other mode, the same block with the narrowed vocabulary:

```
Reply contract — the FINAL line of your reply must be exactly:
status: <ok|blocked|needs-human>; file: <path>
Use `file: -` when this turn produces no file. Put your <=3-line gist
above that line. If any tool call of yours was denied by the permission
system, the status is `blocked`.
```

The status line is anchored to the **end** because `_common.md` requires `[Mode: <name>]` as the reply's first line; the first line is therefore unavailable as an anchor.

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

Reply contract — the FINAL line of your reply must be exactly:
status: <ok|blocked|needs-human>; file: <path>
Use `file: -` when this turn produces no file. Put your <=3-line gist
above that line. If any tool call of yours was denied by the permission
system, the status is `blocked`.
```

(Three values here, not four: this is a `plan` turn, and `failed` is offered only to `execute` turns.)

## Execution

For each turn record, in order:

1. Assemble the subagent prompt (above).
2. Delegate to one general-purpose subagent, requesting the turn's resolved `model` as the delegation-call model override when the platform supports it; otherwise proceed with the inherited model and record that in the run index. One turn = one subagent; never combine turns.
   - **Start the turn watchdog in the same message as the delegation call**, as a background command: `bash <this skill's dir>/scripts/watchdog.sh --deliv <the turn's deliverable path> --desc "<the description given on the delegation call, verbatim>" --mode <the turn's mode>`. Use `--deliv -` for a turn that writes no file. **Every delegation in the run needs its own description**, re-runs and recovery turns included — that string is the watchdog's only key, so two turns sharing one can be confused for each other.
   - The watchdog is what bounds the turn in wall-clock time. The orchestrator is event-driven: it cannot poll, and the only asynchronous wake available to it is the completion of a background command it started itself. Without the watchdog a subagent that never returns leaves the run waiting indefinitely, which is the failure this whole step exists to prevent.
   - It exits with one word — `DONE`, `TIMEOUT`, or `STALL` — and that exit is the wake. Thresholds live at the top of the script; do not pass the delegation call's agent id to it, since it resolves the transcript from `--desc` on its own.
   - Whichever finishes first wins. When the turn's own completion notification arrives first, stop the watchdog task: a verdict that lands after the turn already has a readable status is stale and must be ignored, never re-classified.
3. The subagent writes its deliverable to the run directory as `NN-<mode>.md` and returns only a ≤3-line gist followed, as its **final line**, by `status: <...>; file: <path>` — the status drawn from the vocabulary that turn's contract offered it (`ok|failed|blocked|needs-human` for `execute`, `ok|blocked|needs-human` for every other mode), and `file: -` when the turn produced no file. Read the status from that line and nothing else — prose elsewhere in the reply is not a status.
   - **execute exception**: an `execute` turn edits the actual source files; its file is a short change-report listing the touched paths, not a copy of the work.
   - **`failed`** is emitted only by an `execute` turn: the turn's procedure completed but a planned check (e.g. a test) did not pass, and the failure looks fixable in-repo. On `failed`, the change-report must include a `## Failure report` section with five fields — **Error** (one sentence), **Reproduction** (the exact command), **Error output**, **Target file(s)**, **Context** (language / framework / OS / deps). These are the same fields a `debug` turn needs as input.
   - **A permission denial is `blocked`, never `failed`.** A tool call the permission system refused is not an in-repo fixable failure: routing it into the recovery loop makes the loop re-run a turn that cannot succeed, and each cycle re-spawns subagents that hit the same wall. Denial means the run lacks a capability it needs — a human decision, so stop.
   - **`failed` from a non-`execute` turn is out of contract** (that turn was never offered the value): read it as `needs-human` and stop at step 7. Do not enter the recovery loop — the loop's first move is a `debug` turn fed by a `## Failure report`, which only an `execute` turn produces, so it would be diagnosing a report that does not exist. Report the turn's own gist verbatim so the user can see what it was signalling.
   - If the subagent reports `[BLOCKED: mode-rule <name>]`, relay it verbatim.
4. **A turn that reports nothing is `aborted` — infrastructure failure, not a task outcome.** Two paths reach it:
   - **The watchdog wakes first with `TIMEOUT` or `STALL`.** The turn is over its wall-clock budget, or its subagent stopped generating. Stop the turn and classify it `aborted`. Do not wait for a reply that the watchdog has already established is not coming — waiting for it is the exact failure the watchdog was added to end. Do not try to read the stopped turn's output first: a subagent's output file is its entire transcript, and pulling that into the orchestrator's context to describe a turn that is being discarded anyway can end the run outright. Note the verdict in the run index and move on; the transcript stays on disk for a human to read.
   - **The reply arrives but its final line is not a well-formed `status:` line** (interrupted, killed, or simply off-contract). The turn reported *nothing* about the task.

   In both cases: do not read it as `failed` and do not enter the recovery loop — there is no diagnosable failure, and a `debug` turn would be diagnosing an absence. Instead: **re-run the identical turn exactly once**, watchdog included; if that re-run is also `aborted`, stop the run with `needs-human`. An `aborted` re-run does not consume the originating turn's recovery-cycle cap (that cap counts `failed` cycles).
   - The prompt is identical, but **the re-run's description must not be** — give it a distinct one (e.g. suffix `(re-run)`). The description is the watchdog's only key, and the aborted turn's own record is still on disk and still recent; reuse the string and the re-run's watchdog can latch onto its dead predecessor, whose transcript by definition stopped growing, and report `STALL` immediately against a turn that is working fine.
5. **Chaining**: a later turn receives earlier artifacts by path in its `inputs` and reads the full files itself — never forward a gist as the next turn's input.
6. **Recovery loop** — when an `execute` turn returns `failed`:
   1. Insert a `debug` turn: `inputs` = the plan artifact plus this `NN-execute.md` (with its Failure report); deliverable `NNa-debug.md` = root cause + proposed minimal diff + verification command. If the Failure report's five fields are absent, the `debug` turn returns `needs-human` (it cannot diagnose blind).
   2. Insert a re-execute turn (`mode: execute`, model = the failed turn's model): apply the diff proposed in `NNa-debug.md` and re-run the original planned checks; deliverable `NNb-execute.md`.
   3. If `NNb` returns `ok`, resume the main sequence. If it returns `failed`, run one more cycle (`NNc-debug` / `NNd-execute`).
   4. **Cap**: at most 2 cycles per originating turn (or the active spec's value). When the cap is reached and the turn is still `failed`, escalate it to `blocked` and fall through to step 7.
   - The `debug` turn's model follows the model precedence above (an active spec's `debug` entry applies).
7. On status `blocked` or `needs-human`: stop the run and report verbatim (a dependent step cannot run without its input). Record progress in the run index. This is terminal — `blocked` and `needs-human` never enter the recovery loop.
8. After all turns: summarize the run directory's artifacts and gists.

## Run directory (workspace)

- Create one run directory in the workspace per invocation, e.g. `mode-orchestrator-runs/<run-slug>/` — derive the slug from the input document name.
- Artifacts: `NN-<mode>.md` in order; recovery turns inserted for a failed turn use the suffix form `NNa-debug.md` / `NNb-execute.md` / `NNc` / `NNd`, preserving the originating order. Plus a small index file recording the turn plan, each turn's model (and its decided tier), status/path, the recovery cycle count for any turn that entered the loop, and — for any turn re-run after an `aborted` reply — that it was re-run, which path detected the abort (a missing status line, or the watchdog's `TIMEOUT` / `STALL`), and whether the re-run produced a readable status. Record the `aborted` event even when the re-run then succeeds: an aborted turn writes no deliverable and leaves no other trace, so without this line a post-hoc reader of a stalled or slow run cannot tell that a turn was silently lost and repeated. Recording which path caught it is what makes the thresholds reviewable — a run that keeps hitting `TIMEOUT` on turns that were still working is telling you the budget is too tight, and that is invisible if every abort looks the same. This index is an artifact index for inspection, not a resumable scheduler.
- These are runtime artifacts; do not commit them.

## Context discipline

- This skill reads the whole input document to generate the turn plan.
- Downstream, subagents receive their inputs as **file paths only** — never inline the document's raw content into a subagent prompt. This keeps each subagent's context clean and isolated.
- Prefer invoking the skill in a fresh session. The input document is bounded structured content, but a clean session keeps the orchestration context lean.

## Out of scope

- No rollback / checkpoint / worktree around `execute` turns — working-tree safety is the user's git hygiene (and CLAUDE.md) concern, not this skill's.
- No mid-run interactive handoff and no resume scheduler.
- No helper scripts beyond the turn watchdog (`scripts/watchdog.sh`) — prompt assembly and the Step 0 gate are done in-prompt. The watchdog is a script on purpose: it is the one part of a turn that must behave identically every time, and re-typing it per turn would put the wall-clock bound back under the same improvisation it exists to bound.
- No zero-decomposition of an unstructured task — the todolist must be in the input.

## Known residual risks

- Over a long run the orchestration context grows; this is mitigated by gist-only returns, short pipelines, and passing inputs by path. Recovery cycles add turns and enlarge context further; the per-turn cycle cap keeps this bounded.
- A `debug` turn's quality depends on the failed turn's Failure report being complete; the five-field contract (and the `needs-human` fallback when fields are missing) mitigates but does not eliminate this.
- A change-report is self-attested: an `execute` turn's account of which file it edited (and whether it edited at all) is not independently guaranteed. Trust the run's end state (re-run the planned check), not the report's authorship claims.
- The status line is prompt-level, not machine-enforced — a subagent can omit or malform it. The design is deliberately fail-safe in one direction: an absent line reads as `aborted` (abnormal), never as success, so the cost of non-compliance is one extra re-run rather than a silently accepted turn. It gives no protection in the other direction — a subagent that emits `status: ok` without doing the work is indistinguishable from one that did (same limitation as the self-attestation risk above).
- The status-line contract only classifies turns that reply; the watchdog is what covers a subagent that never returns. That split means neither half is sufficient alone, and the watchdog's own guarantee is bounded: it proves a turn exceeded a budget, never that the turn was wrong. Its thresholds are wall-clock guesses, so they are calibrated to over-wait rather than cut a slow turn short.
- `STALL` cannot distinguish a hung subagent from one that is thinking or sitting in a long tool call — the transcript looks identical in all three. The threshold is therefore set well above the longest tool call a turn is expected to make, which means a genuine hang is detected late rather than not at all. Lowering it trades that delay for turns killed while still working.
