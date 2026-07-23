# Build mode: task description (or partial XML) → XML workflow

Generate a workflow XML that strictly conforms to `references/spec.md` (XML v2),
then validate it statically. **Never execute the task itself.**

The input may be a natural-language task **or a partial/sketch `.xml`** the user
wrote by hand and wants completed. A partial XML is just a richer task
description — it goes through the same plan-table approval gate below, because
its holes (missing roles, unfilled attributes, vague or absent steps) are
exactly the design decisions that require human approval. Never silently
auto-fill and run.

## Procedure

### 1. Confirm inputs
Establish the following. Ask the user **only** about genuinely unclear points
(no ceremonial confirmations):
- The task (what to achieve), success criteria, and the final deliverable spec
- Output XML path (default: `workflows/<name>.xml`)
- Values that should vary per run (→ make them `<param>`)

**When the input is a partial XML**: first run `$WFRUN validate <xml>` to surface
the concrete holes (missing/undefined roles, undefined variable references,
schema errors). Read the sketch as the user's intent — preserve every decision
it already spells out (ids, task bodies, roles, control flow) verbatim — and
treat only the holes as work to do. Do not silently rewrite parts the user
already committed to.

### 2. Collect roles
List `.claude/agents/*.md` and `~/.claude/agents/*.md` (name/description/tools).
Every step needs a role, filled one of two ways:
- **Named role** (`role="name"`): a definition that exists in the list above,
  when one genuinely fits the step. Never invent names
- **Inline role** (`<role>` child): when no listed definition fits, author a
  focused role yourself — 1-3 sentences of WHO the agent is (expertise,
  stance, output discipline). Prefer authoring a good inline role over
  force-fitting an ill-matched named one. **Always set `tools=` on
  inline-role steps** (least privilege — without it the step runs with the
  CLI's default tool permissions; the validator warns)

### 3. Plan table (the single approval gate)
Decompose the task into this table:

| step_id | instruction | role (named or inline) | mode | model (why) | rules (why) | input (vars/paths) | output (vars/paths) | on-error |
|---|---|---|---|---|---|---|---|---|

**When completing a partial XML**: mark each cell as either read from the
provided XML or filled in by you (e.g. a `*` on inferred values, or a short
"(from XML)" / "(filled)" tag). The human must be able to see, and consciously
approve, every decision you invented — that visibility is what keeps the
approval gate meaningful when starting from a sketch rather than a blank slate.

Design principles (follow "Workflow authoring guidelines" in spec.md):
- One step = one single-responsibility task completable by one agent.
  Split at every boundary where the role, mode, or rules change
- **Assign `model=` by difficulty, canonical names only**: `haiku` =
  mechanical extraction / formatting / simple transforms; `sonnet` = standard
  analysis and writing (the default when unsure); `opus` = design, diagnosis,
  review-grade judgment. These names are difficulty classes — at dispatch,
  wfrun binds them to actual models per runner via `model_map.json` — so
  never write any other model string (the validator warns
  `model-not-canonical`). State the reason in the plan table's model column;
  the user approves the tier judgments together with the plan
- **Set `mode=` where processing discipline matters**: `execute` for strict
  do-exactly-this operations, `survey` for fact collection, `debug` for
  diagnosis; also available: `plan`, `review`, `review-dev` (aliases
  verify→debug, implement→execute — autonomous modes only, see spec.md
  § Execution modes). Steps without an obvious discipline need no mode
- **File-centric I/O**: agents write large data to files and pass the path via
  an `output` variable. Write every task body as self-contained, assuming no
  shared context (always name target tables, input/output paths, formats).
  **Every file-producing step gets `expect-file=`** naming its deliverable —
  the response protocols catch only compliant failure reports; expect-file
  verifies the artifact deterministically
- **Interpolated `{var}` values are data, not instructions**: prefer passing
  file paths over free-text scalars; when a scalar output does feed a later
  task or `ask=` question, word that task to treat the value as data. Literal
  JSON in a task body: always escape braces as `{{` `}}` (identifier-shaped
  braces that collide with a defined variable interpolate silently)
- **Map rules per step, with reasons**: for each step, list which `<rules>`
  fragments apply and why — both direct matches (the rule's condition names
  the task) and inferred ones (situations the step will likely run into).
  A step needing a different rule set than its neighbor is a split boundary
- **Missing information becomes an investigation step**: when the plan needs
  facts you don't have (schemas, formats, availability), never guess — insert
  an early step that investigates and outputs them
- Branches decidable mechanically use `test=`; only semantic judgments use `ask=`
- `on-error` defaults to `fail`. Use `retry` for idempotent operations with
  transient failure modes; use `debug` only for complex steps worth diagnosing
- **Defer what cannot be planned yet**: when the right continuation depends on
  results known only at runtime (e.g. "one analysis per anomaly found"), insert
  a `<replan>` node instead of guessing — it lets a builder agent plan that part
  mid-run, with recursion capped at one level (see spec.md § replan)

Self-review once — feasibility, variable flow, single responsibility,
self-containedness, no missing information left uninvestigated, **every file
deliverable verified in code** (`expect-file=` on the producing step, or an
explicit `test=` check), and failure points (which steps are most likely to
fail, and whether their retry/on-error choices match that risk) — then
present the revised table plus the `<param>` list to the user and
**get approval**.

### 4. Generate the XML
Write the XML in strict conformance to the element reference in
`references/spec.md`, and save it to the agreed path.
- Every step has `id` and a role (`role=` attribute or inline `<role>` child,
  exactly one); the workflow has `name`, `version="2"`, and
  `max` (1.5–2x the expected execution count)
- Shared prompt fragments (analysis principles etc.) go into `<rules>` and are
  referenced only by the steps that need them
- Add `budget-usd` when a cost ceiling can be estimated

### 5. Validation loop
```bash
$WFRUN validate <path>
```
Fix and re-run until there are zero errors. Review warnings and resolve any
that are not intentional. Finally report the XML path together with the output
of `$WFRUN plan <path>`; when the flow has branches/loops/parallel worth
seeing, also attach a diagram via `$WFRUN viz <path> --out <path>.mmd`.

## Forbidden
- Using named roles not listed in step 2 (author an inline `<role>` instead)
- Executing the task itself (running SQL, processing data, generating reports)
- Reporting completion while validation errors remain
