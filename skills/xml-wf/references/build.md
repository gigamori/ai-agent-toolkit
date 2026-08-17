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
List `.claude/agents/*.md` and the user agents dir's `agents/*.md`
(`$CLAUDE_CONFIG_DIR` or `~/.claude`, project overwrites env overwrites default
on a name collision) (name/description/tools).
A role is **optional**. Give a step one only when WHO does the work actually
changes the outcome:
- **No role** (a fine default): when `mode=` and `rules=` already fix the
  discipline, leave it out. Do NOT invent a generic persona ("You are a
  careful engineer") just to fill the slot — it spends tokens and steers
  almost nothing. **Set `tools=`** on such steps (least privilege — there is
  no role frontmatter to inherit from; the validator warns
  `tools-not-inherited`)
- **Named role** (`role="name"`): a definition that exists in the list above,
  when one genuinely fits the step. Never invent names
- **Inline role** (`<role>` child): when no listed definition fits but real
  expertise, stance, or output discipline is needed, author a focused role
  yourself — 1-3 sentences of WHO the agent is. Prefer this over force-fitting
  an ill-matched named one. **Always set `tools=`** here too (same reason)

### 3. Plan table (the single approval gate)
Decompose the task into this table:

| step_id | instruction | role (named / inline / none) | mode | model (why) | rules (why) | input (vars/paths) | output (vars/paths) | on-error |
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
  review-grade judgment. State the reason in the plan table's model column;
  the user approves the tier judgments together with the plan. The rules that
  constrain this choice — why these are classes and not models, which concrete
  models each layer may bind, and what has been measured about model floors —
  are in **§ Model selection** below
- **Decision requests (`DECISION:`) need no authoring** — the protocol is
  injected into every step, because forks are not foreseeable (an attribute
  saying "this step may fork" could only be set where someone already
  predicted the fork). What IS the builder's call:
  - **`decider=`** (workflow or per-step): default `human` stops the run at a
    fork for a `resume --answer`; `llm` lets an adjudicator (`decider-model=`,
    default `opus`) settle it unattended — capped at 2 rulings per step visit,
    with irreversible / outward-facing / goal-changing forks escalated to a
    human regardless. Declare `llm` only where unattended operation is worth
    a ruling made under the same contract the work was produced under
  - **Give heavy fork-prone steps an `expect-file`**: an answered fork skips
    the re-run (form (a)) only when the step's deliverable is verifiable —
    declared in `expect-file` and present. Without it, every ruling re-runs
    the whole step
  - The firing conditions are in the injected text, not the task: following a
    recommendation already recorded upstream is not a fork, a blocking error
    is `ERROR:`, and the request fires only when the step cannot proceed
    without a ruling. Task text never needs to (and should not) restate this
- **Do not put a fork-prone step on a model below the raising floor** — the
  floor and the evidence for it are in **§ Model selection** below
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
- Every step has `id` and at most one role form (`role=` attribute or inline
  `<role>` child — never both, and neither is valid); the workflow has `name`,
  `version="2"`, and `max` (1.5–2x the expected execution count)
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

## Model selection

Canonical for both this skill and `mode-orchestrator` — that skill's
`AUTHORING_CONTRACT.md` points here rather than restating the lists, so they
cannot drift.

**The rule: a concrete model ID is never written at runtime.** `haiku` /
`sonnet` / `opus` are difficulty *classes*, not models. wfrun binds a class to
an actual model per runner at dispatch via `model_map.json`, so the
provider-qualified name belongs in that table (config) or in this section
(documentation) — never in a step. `decider-model=` may name a
provider-qualified model directly — spec.md and run-pi.md both grant it "any
name the runner's model table accepts", because a pi adjudicator has to be
nameable and pi's models are not in the canonical vocabulary. That is the one
place such an ID belongs in a workflow.

**What `wfrun validate` checks depends on the backend.** Under `--backend pi`
it resolves every `model=` and every adjudicator an `llm` decider would
actually be sent, then matches each against `pi --list-models`: a name matching
nothing is an **error** (`pi-model-unavailable`), a non-canonical `model=` is a
warning (`model-not-canonical`), and an unreadable catalog is the warning
`pi-model-unverified` rather than a pass. The match mirrors pi's own resolver —
exact on `id`/`provider/id`, else substring on `id` — so `opus` reaches
`opus[1m]` and the default adjudicator is not flagged. Under `cc` **no model
name is checked**: those runs stay inside the canonical vocabulary, which
`model_map.json` binds to claude CLI names, so there is no catalog for a name
to be missing from.

**Approved candidates per layer (2026-08-17).** The split records which role
each model was measured in — it is not a cost ranking, and fitness for one
layer never transfers to the other.

| Layer | What runs on it | Candidates |
|---|---|---|
| Orchestrator | a step, a delegated turn, an adjudication | `opus`, `gpt-5.6-terra`, `gemini-3.6-flash` |
| Measurement | an eval or sampling harness, where per-sample cost decides how large N can be | `sonnet`, `gpt-5.6-luna`, `gemini-3.5-flash-lite` |

Both tables in `model_map.json` currently bind all three classes to the
identity (Anthropic) names, so choosing a non-Anthropic candidate means editing
that table's values.

**Measured floors.** Two capabilities are separate and must be chosen
separately: *raising* a `DECISION:` fork (noticing the task is under-specified
— spec.md § Execution semantics) and *ruling* on one. Raising is the harder
act, and its failures are silent: the model picks a reading and returns ok.

| Model | Raising a fork | Ruling on one | Measured |
|---|---|---|---|
| `haiku` | 0/10 (silent, samples disagreed) | — | 2026-08-12, byte-identical prompts |
| `sonnet` | 9/10 | — | 2026-08-12, same prompts |
| `google/gemini-3.1-flash-lite` | 0/1 (silent, the haiku failure exactly) | 40/40 as `decider-model` | 2026-08-13 (`scripts/evals/adjudicator_smoke.py`) |
| `google/gemini-3.5-flash-lite` | 3/3 well-formed | — | 2026-08-13, one ambiguous fixture |

Reading these numbers:

- **`sonnet` is the floor for raising** where a step's inputs or rules might be
  ambiguous. The N above are far too small to be rates; what they establish is
  that the floor is a property of the **model**, not of the backend — so a pi
  workflow needs its own floor chosen the same way. **Choosing a cheap model
  for a fork-prone step does not make the fork cheaper; it makes it invisible.**
- **A measurement licenses a role, not a family name.** The two flash-lite
  variants hold opposite records, so neither name generalizes: `3.5-flash-lite`
  raised 3/3 here yet was **rejected for the orchestrator layer on 2026-08-17**
  (driving a mode-orchestrator run it set no timeout on any of its 3
  delegations and dropped every other element that harness reference requires),
  while `3.1-flash-lite` raised nothing yet ruled 40/40. Read every model name
  in this section as a claim about the role it was measured in, and re-measure
  before reusing one in the other role.
- `adjudicator_smoke.py`'s default moved to `3.5-flash-lite` on 2026-08-17 to
  match the measurement-layer list, so the 40/40 above belongs to
  `3.1-flash-lite` and a re-run is a new sample, not a continuation of it.

## Backend compatibility

When the request is to make an existing workflow **pi-compatible** — typically
relayed from a `run --backend pi` startup rejection — the attributes
`references/run-pi.md` lists as unsupported are **holes to fix, not decisions
to preserve**. The verbatim rule in step 1 does not cover them; everything else
in the input still does.

Two attributes are affected. Apply the replacements in `run-pi.md`
("Replacing `schema=`" and "Replacing `on-error=\"debug\"`") — do not invent
your own.

Both replacements **reduce a guarantee**, so surface each one in the plan table
(step 3) as its own row, saying what the step loses:

| id | change | what it costs |
|---|---|---|
| `s2_count` | `schema=` → `expect-file=` + `output-type="value"` | shape guarantee → existence guarantee; the value's format is no longer enforced (moves to `ask=`, a likelihood) |
| `s4_build` | `on-error="debug"` → `retry="2"` | diagnosis before retry is gone; a retry now repeats the identical attempt |

A conversion that silently swaps these attributes is worse than no conversion:
the workflow keeps running and the guarantee it was written against is gone.
The approval gate exists so the user chooses that trade, not discovers it.

## Forbidden
- Using named roles not listed in step 2 (author an inline `<role>`, or leave
  the role out, instead)
- Inventing a filler persona for a step whose discipline `mode=`/`rules=`
  already sets — omit the role instead
- Executing the task itself (running SQL, processing data, generating reports)
- Reporting completion while validation errors remain
- Silently rewriting a backend-unsupported attribute without showing the
  guarantee it costs (see Backend compatibility)
