# XML Workflow Control-Structure Specification v2 (canonical)

Defines the definition format and execution semantics for workflows that
decompose a task into a sequence of single-responsibility steps, each executed
as an **independent subagent** (a `claude -p` headless process).

The fundamental change from v1 (the Roo-Code-era LLM-orchestrator design):
**the orchestrator is Python (wfrun), not an LLM**. The XML is not something an
LLM reads — Python parses it and executes it deterministically. The defensive
constraints that filled over half of the v1 prompt (NO SKIP, no optimizing, no
fabricating variables, …) became structurally unnecessary and were removed.
An LLM is involved in exactly four places:

1. **Step execution** — an independent `claude -p` subprocess per `<step>`
   (complete context separation)
2. **LLM condition judgment** — the `ask=` attribute (structured output forces
   a boolean + reason)
3. **Failure diagnosis** — the debug role on `on-error="debug"` (optional)
4. **Dynamic replanning** — a builder role generating a continuation workflow
   at a `<replan>` node (optional, one level deep)

Every step runs under an explicit **role** (WHO the agent is) and optionally an
execution **mode** (HOW it processes; a bundled snapshot of the role-mode
prompt set). Both are injected into the step prompt by wfrun — `--agent` is
never passed to the CLI, so what the subagent sees is exactly what the prompt
file shows.

## Design principles

1. **Determinism**: control flow, variable resolution, and error policy are
   Python code. The same XML + the same inputs walk the same execution path
2. **Context separation**: steps share no conversation context. Hand-offs are
   variables (scalars) and files only
3. **File-centric I/O**: large data always moves as file paths. Task bodies
   spell out concrete paths
4. **Verifiability**: the schema is closed (unknown elements/attributes are
   immediate errors). `wfrun validate` checks statically before execution
5. **Auditability**: every prompt, response, and judgment is recorded under
   `runs/`, and a run can be resumed from the point of failure

## Notation rules

- Identifiers and scalar settings are **attributes only** (v1's dual
  attribute/child-element notation is gone)
- Only long-form blocks are child elements: `<task>` bodies, inline `<rules>`
  bodies, and the `<then>`/`<else>`/`<do>` containers
- Variable references are `{name}` (Python identifiers). Literal braces are
  escaped as `{{` `}}` (JSON fragments may be written as-is when the braced
  content is not identifier-shaped)

## Element reference

### `<workflow>` (root)

| Attribute | Required | Meaning |
|---|---|---|
| `name` | ✔ | Workflow name (used in the run dir name) |
| `version` | ✔ | Fixed `"2"` |
| `max` | ✔ | Cap on total step executions (including loops, branches, and post-retry re-runs). Runaway protection |
| `budget-usd` | - | Cumulative cost ceiling (USD). When exceeded, execution stops before the next step starts |

### `<param>` (direct child of workflow)

Workflow arguments, injected at run time with `wfrun run wf.xml -p key=value`.
Replaces v1's "external `<set>`".

| Attribute | Required | Meaning |
|---|---|---|
| `name` | ✔ | Variable name |
| `required` | - | `"true"` = error when not supplied |
| `default` | - | Default value when not supplied |

### `<rules>` (direct child of workflow)

Prompt-fragment definition. Injected as `<rules id="...">content</rules>` at
the top of a step's prompt only when that step's `rules` attribute references it.

| Attribute | Required | Meaning |
|---|---|---|
| `id` | ✔ | Reference name |
| `src` | - | External file path (relative to the XML file). When omitted, the element body is the inline content. Specifying both is an error |

### `<step>`

One task executed by one agent. Children: `<task>` (required) holds the
instruction body; `<role>` (see below) may hold an inline role definition.

| Attribute | Required | Default | Meaning |
|---|---|---|---|
| `id` | ✔ | - | Unique identifier. Key for logs, the steps/ directory, and resume |
| `role` | (✔) | - | Named role: a `.claude/agents/*.md` definition (project first, then `~/.claude/agents/`) whose **body is injected** as the `<role>` block. Exactly one of `role=` or an inline `<role>` child is required |
| `mode` | - | - | Execution mode (see "Execution modes" below) |
| `model` | - | role frontmatter | Canonical difficulty name — `haiku`/`sonnet`/`opus` only (step attribute wins over the named role's frontmatter). Bound to an actual model per runner at dispatch (see "Model resolution"); other strings pass through but warn (`model-not-canonical`) |
| `effort` | - | - | `low`…`max` (forwarded to `--effort`) |
| `output` | - | - | Variable name that receives the result |
| `output-type` | - | `file` | See below |
| `schema` | - | - | JSON Schema (inline, or `@path` for a file reference). Forwarded to `--json-schema`; forces structured output |
| `rules` | - | - | Comma-separated rules ids to inject |
| `tools` | - | role frontmatter | Forwarded to `--allowedTools` (e.g. `"Read,Write"`; step attribute wins) |
| `expect-file` | - | - | Comma-separated paths (`{var}`-interpolated; relative to the XML dir) that must exist after the step. Missing = step failure (retry / on-error apply). The deterministic deliverable check — a compliant-looking response without the artifact is caught |
| `retry` | - | `0` | Deterministic retry count (re-run with the identical prompt) |
| `timeout` | - | `600` | Seconds. Process is killed on overrun → error |
| `on-error` | - | `fail` | `fail` (stop immediately) / `ignore` (record and continue) / `debug` (debug-role diagnosis) |

**The `<role>` child** — when no suitable `.claude/agents` definition exists,
the builder authors the role inline:

```xml
<step id="s2" mode="survey">
  <role>You are a meticulous fact collector who reports only what the data shows.</role>
  <task>...</task>
</step>
```

An inline role has no frontmatter, so `model`/`tools` come only from the step
attributes (CLI defaults otherwise). Specifying both `role=` and a `<role>`
child, or neither, is a parse error.

**Meaning of output-type**:
- `file`: save the agent's final response body to `runs/<ts>/outputs/<id>.md`
  and store **that path** in the variable. For long responses (reports etc.)
- `value`: store the response in the variable as a **scalar**. With `schema`,
  a top-level single-property object auto-unwraps to its value (e.g.
  `{"line_count": 3}` → `3`); multiple properties store as a JSON string

**Deliverable-file principle**: when the agent itself writes files
(recommended), name the output path in the task body and use
`output-type="value"` + "return only the file path as your final response" to
pass the path downstream. Write paths **relative to the current directory**
and make sure agents do not absolutize them (the subprocess cwd is the
directory containing the XML file). Set `expect-file=` on such steps: the
ERROR:/[BLOCKED: protocols only catch *compliant* failure reports, while
expect-file verifies the deliverable itself.

### `<replan>` (dynamic continuation, one level deep)

Defers part of the plan to run time: a **builder agent** receives the task
body (with `{var}` interpolation, so results so far can be embedded) plus the
current variable table, and returns a **continuation workflow XML**. wfrun
validates it programmatically and executes it inline. Use it when the right
continuation depends on results known only mid-run (e.g. "one analysis step
per anomaly found").

The required child `<task>` describes what the continuation must achieve.
The builder's role follows the same contract as `<step>`: exactly one of
`role=` or an inline `<role>` child. There is no `mode=` (the builder prompt
is a fixed XML-only contract that a mode would interfere with).

| Attribute | Required | Default | Meaning |
|---|---|---|---|
| `id` | ✔ | - | Unique identifier (shares the step id namespace) |
| `role` | (✔) | - | Builder role that generates the continuation (or an inline `<role>` child) |
| `model` / `effort` | - | role frontmatter | Forwarded like `<step>` |
| `max-steps` | - | `20` | Cap on the continuation: its `max` must not exceed this, and its executed steps are additionally capped here |
| `outputs` | - | - | Comma-separated variable names the continuation must define (checked after it runs; missing = failure) |
| `retry` | - | `0` | Regeneration attempts when the produced XML fails validation (validator errors are fed back to the builder) |
| `timeout` | - | `600` | Seconds for the builder call |
| `on-error` | - | `fail` | `fail` / `ignore` (a failed replan leaves `outputs` unset). `debug` is not meaningful here |

Semantics:
- The builder runs with read-only tools (`Read,Glob,Grep`), receives the spec
  path, the list of available named roles, and the variable table, and must
  reply with a complete `<workflow>` document
- The continuation is validated with the child ruleset: **no `<replan>`
  (recursion) and no `<param>` allowed**; named roles must exist (inline
  `<role>` bodies are always allowed); variables already defined count as
  defined. Its `max` must be ≤ `max-steps`
- On success the XML is saved to `runs/<ts>/replans/<id>_<nn>.xml` and executed
  inline: **variables are shared** with the parent, its rules are merged for
  the duration, and its step executions count toward the workflow's `max`
- Resume replays a successful generation from the recorded XML instead of
  calling the builder again

### `<set>`

| Form | Meaning |
|---|---|
| `<set var="x" value="literal or {interp}"/>` | Interpolation only (no expression evaluation) |
| `<set var="n" expr="{n} + 1"/>` | Safe expression evaluation (below) |

Exactly one of `value`/`expr`. All variables are global (only `<each>` loop
variables are loop-scoped).

**Safe expression evaluation**: `expr` / `test` are evaluated via an AST
allowlist. Available: literals, arithmetic, comparisons, `and/or/not`, `in`,
lists, and the functions `len/int/float/str/abs/min/max/round`. Attribute
access, subscripts, comprehensions, and any other calls are rejected by static
validation. Quote string variables in comparisons: `test="'{status}' == 'ok'"`

### Control structures

```xml
<seq> ... </seq>                          <!-- sequential (implicit seq directly under workflow) -->

<if test="int({count}) > 3">              <!-- Python-expression judgment -->
  <then> ... </then>
  <else> ... </else>                      <!-- else optional -->
</if>

<if ask="Does report {report} meet the success criteria?">   <!-- LLM judgment -->
  <then> ... </then>
</if>

<while test="..." max="10"> <do> ... </do> </while>   <!-- max required. ask= also allowed -->

<each items='["a","b"]' as="x"> <do> ... </do> </each>  <!-- JSON array ({var} interpolation allowed) -->
<each glob="output/*.csv" as="f"> <do> ... </do> </each> <!-- sorted glob -->
<each range="5" as="i"> <do> ... </do> </each>           <!-- 0..4 -->

<parallel max-workers="2">                 <!-- children: steps only. No inter-branch variable deps -->
  <step id="a" .../> <step id="b" .../>
</parallel>
```

- Exactly one of `test` and `ask`. The `ask` question is `{var}`-interpolated
  and judged by **haiku** (changeable via the `ask-model` attribute) with the
  structured output `{answer: boolean, reason: string}`. The judgment agent is
  allowed only the `Read` tool, so it can actually read file paths named in
  the question. The reason is recorded in events.jsonl
- If a `<while>` condition is still true after `max` iterations, a warning
  event is recorded and execution **continues** (it is not a failure)
- `<each>` provides the loop variable `{x}` and `{x_index}` (0-based). Both
  revert to undefined after the loop
- `<parallel>` child steps cannot reference each other's `output` (validate
  detects this). Variables are committed as each step completes

## Execution modes (`mode=`)

Derived from the role-mode plugin's `prompts/modes/` (snapshot 2026-07-19 into
`scripts/wfrun/modes/`, then **rewritten for batch workflow execution** —
maintained independently). Setting `mode=` on a step injects the `mode:<name>`
declaration, the mode body, and the all-modes rules (`_common.md`); the
framework header (`_meta.md`) is injected for every step, mode or not.

`_meta.md` declares the prompt axes in tag vocabulary and their precedence:

> **Mode > Rules > Task > Role** — constraints (mode, rules) over the
> instruction (task) over the persona (role). If a mode or rules constraint
> truly blocks the task, the agent replies with a single
> `[BLOCKED: mode-rule <name>]` line and stops (a detected error, see below).
> Files at paths the task names are the step's own mode-output — writing them
> is always allowed, whatever the mode.

Available modes (**autonomous only** — the plugin's interactive modes
ask/brainstorm/discuss/organize need a live human exchange and are not
bundled): `debug`, `execute`, `plan`, `review`, `review-dev`, `survey` —
plus the aliases `verify` → debug and `implement` → execute (the alias picks
the file; the declared name is preserved in the prompt). Unknown names are a
validate error (`mode-unknown`).

Practical guidance: `execute` suits strict do-exactly-this steps (operations,
file writes), `survey` suits fact-collection steps, `debug` suits diagnosis
steps. Steps without `mode=` get no mode rules — only `_meta` and the
`<role>` block.

**`[BLOCKED:` responses**: a response whose first non-empty line starts with
`[BLOCKED:` is a detected error (same wiring as the ERROR: protocol —
on-error applies, the blocked line becomes the recorded reason — except that
the deterministic `retry` is skipped: an identical prompt would be refused
identically, so a refusal goes straight to on-error handling). Legacy
`[Mode: ...]` first lines are still stripped defensively before any
classification or value extraction.

**Trust boundary**: modes are processing discipline, not a defense against
the task author — a path the task names is writable in any mode, so a task
worded as an edit turns any step into an edit step. The trust anchor is the
human-approved plan (and the `tools=` grant, which *is* deterministic; the
validator warns `mode-write-tools` when a non-writing mode gets write-capable
tools).

## Execution semantics (what wfrun does)

### Prompt composition (deterministic assembly, two channels)

```
system channel (run-cc: --append-system-prompt)
  _meta.md (framework header)      ← always (every step has a role)
  <role>...</role>                 ← named definition's body, or the inline <role>
  mode:<name> + mode body          ← only when mode= is set
  _common.md (all-modes rules)     ← only when mode= is set
  <rules id="...">...</rules>      ← only those referenced by the rules attribute

user channel
  task body ({var}-substituted)    ← interpolation failure = undefined reference = immediate error
  response protocol (run-llm only)
  guardrails (fixed text)
```

The constraint layers (role/mode/rules) ride the **system channel** in run-cc
via `--append-system-prompt` (append, not replace — the CLI's default tool-use
scaffolding is kept); the user prompt carries only the task and the trailing
guardrails trigger. run-llm joins both parts into the single prompt file (the
Agent tool has no system-prompt input — that is the platform ceiling there).

Because the role body travels inside the prompt, `--agent` is never passed to
the CLI; the named role's frontmatter `model`/`tools` are resolved by wfrun
and passed as explicit `--model`/`--allowedTools` flags (step attributes win).

**Guardrails** (the sole survivor of the v1 defensive prose; appended to every
step):

> You are an agent executing a single step within a workflow.
> 1. If an error blocks you, do NOT self-repair and do NOT fabricate
>    substitute results. Return a concise technical report starting with
>    "ERROR:" as your final response and stop. Recovery belongs to the
>    orchestrator.
> 2. Do not write to any file that the task does not explicitly name.
> 3. Do not substitute, abbreviate, or summarize the deliverable. Produce
>    exactly the artifacts at exactly the paths the task specifies.

### Model resolution (`model_map.json`)

`model=` names a **difficulty class**, not a deployment: the canonical
vocabulary is `haiku` (mechanical extraction / formatting / simple
transforms), `sonnet` (standard analysis and writing — the default when
unsure), `opus` (design, diagnosis, review, replan building). Builders judge
against these anchor words; `scripts/wfrun/model_map.json` binds them to the
models that actually run, **once, deterministically, at dispatch**:

- the `cc` table covers everything dispatched through the claude CLI —
  run-cc steps, `ask=` judgments (both runners), debug diagnoses, replan
  builders — and must hold claude CLI model names
- the `llm` table covers run-llm step delegation: `wfrun prompt` prints the
  resolved name on the dispatch line (as `model=X (mapped from Y)` when a
  mapping applied) and the orchestrator passes it through verbatim

The bundled map is the identity (zero-config = current behavior). Unmapped
names pass through. Applied mappings are recorded as `model-map` events;
a broken map file is a startup error, never silently ignored. Resume note:
replayed steps are untouched, but steps that actually re-run resolve against
the map as it is at resume time.

### Error detection (priority order)

1. `claude` non-zero exit / `is_error` in the result JSON
2. `timeout` exceeded (SIGKILL)
3. Response body starts with `ERROR:` (guardrail protocol)
4. Response body's first line starts with `[BLOCKED:` (mode/rules refusal —
   the line is recorded as the error reason; deterministic retry is skipped)
5. `schema` was given but no structured output came back
6. A path named in `expect-file` does not exist after the response

Items 3–4 are single-token-prefix protocols and catch only *compliant*
refusals (an agent that narrates before the marker, or half-works then
apologizes, classifies as success). They are likelihood levers, not gates —
the deterministic layer is items 5–6 plus downstream `test=` checks; give
every file-producing step an `expect-file`.

### Error handling (modernized ADP)

1. **Deterministic retry**: re-run `retry` times with the identical prompt
   (absorbs transient failures)
2. After retries are exhausted, `on-error="debug"` hands the failing step's
   definition, sent prompt, execution result, and stderr to the **debug role**
   (`.claude/agents/debug.md`, injected as a `<role>` block like any step
   role), which diagnoses via the structured output
   `{action: RETRY|FAIL, reason, fix_instruction?}`
   - `RETRY`: re-run **exactly once** with the fix instruction appended to the
     original task. A second failure fails unconditionally
   - v1's `RESOLVE` (synthesizing substitute output) was removed as a
     fabrication risk
3. `fail`: stop execution. The full context is saved under runs/, and **a
   human (or the xml-wf skill) fixing the cause and running `wfrun resume` is
   the primary recovery path**
4. `ignore`: record the failure and continue (the output variable stays
   unset; do not overuse)

### Run dir and resume

```
runs/<name>_<YYYYMMDD-HHMMSS>/
├── workflow.xml        # snapshot taken at run time (resume reads this)
├── params.json
├── state.json          # {status, vars, step_count, cost_usd, error}
├── events.jsonl        # append-only execution log (step/cond/test/set/debug/replan/run)
├── outputs/<id>.md     # response bodies for output-type=file
├── replans/<id>_<nn>.xml  # validated replan continuations
└── steps/<id>_<attempt n>/
    ├── system.md       # system-channel part (role/mode/rules) actually sent
    ├── prompt.md       # user-channel part (task/protocol/guardrails)
    ├── result.json     # raw claude -p JSON output
    └── stderr.log
```

**Resume** (event sourcing): `wfrun resume runs/<dir>` replays the success
events in events.jsonl in execution order, reusing step results and ask
judgments (without re-running) as long as they match the record. Actual
execution starts at the first mismatch or unrecorded point. To fix a failing
step, edit `workflow.xml` inside the run dir before resuming (do not change
the definitions of already-succeeded parts).

### Cost and limit management

- `total_cost_usd` of every step, ask judgment, debug diagnosis, and replan
  generation is accumulated and recorded in state.json
- `max`: counted at each step-execution start (replays count too). Exceeding
  it stops the run
- `budget-usd`: checked before each next step starts. A step already in flight
  is not interrupted
- Note: under subscription auth `total_cost_usd` is indicative only

## CLI reference

```
wfrun validate <wf.xml> [--json] [--no-role-check]
              [--as-child] [--defined-vars VARS_JSON]   # static validation (error = exit 1)
wfrun run <wf.xml> [-p k=v ...] [--run-dir D] [--runs-root runs]
          [--permission-mode acceptEdits]               # validate → execute
wfrun resume <run_dir> [--base-dir D] [--permission-mode ...]
wfrun plan <wf.xml>                                     # print the step tree (no execution)
wfrun viz <wf.xml> [--out FILE]                         # mermaid flowchart of the control flow
```

- `--permission-mode` is forwarded only to steps whose resolved tools can
  write (Write/Edit/Bash/… — or when tools are unrestricted); read-only steps,
  ask judgments, and replan builders run without it. Workflows that write
  files usually need `acceptEdits` (the default follows the permissions in the
  project's `.claude/settings.json`). Give survey/review steps a read-only
  `tools=` so the widened permission never reaches them
- Named-role resolution, rules-relative paths, and the subprocess cwd are all
  based on **the directory containing the XML file**
- That directory must not be inside `~/.claude` (the CLI demands interactive
  write approval under its own config tree, so file-writing steps would fail;
  `wfrun run`/`resume` reject this at startup — copy bundled examples to a
  normal project directory before running them)
- The helper subcommands `interp` / `eval` / `ask` / `prompt` / `record` exist
  for LLM-orchestrated execution; see `references/run-llm.md`
- `viz` renders branch diamonds, loop-back edges, and parallel fan-out for
  docs and the build-mode approval gate. Its labels carry control-plane facts
  only (ids, roles, modes, models, conditions — never task bodies). `plan`'s
  ascii output is the run-llm control skeleton and its format is protocol;
  `viz` is the presentation surface that may evolve freely

### What static validation covers

Schema validation (unknown element/attribute rejection, required attributes,
id uniqueness, test/ask exclusivity, role=/​<role> exclusivity, while max),
variable-flow validation (execution-order trace, undefined reference = error,
defined on one branch only = warn), named-role existence (`role-missing`),
mode existence (`mode-unknown`), rules src existence, schema JSON parsing,
expr/test allowlist checks, parallel constraints, replan constraints
(child-mode rejection of `<replan>`/`<param>`, declared-output shadowing =
warn), expect-file variable references, and warnings (`on-error=ignore`,
undersized `max`, `inline-role-no-tools` — an inline `<role>` without
`tools=` runs with the CLI's default tool permissions — and
`mode-write-tools` — a non-writing mode (survey/plan/review/review-dev)
combined with write-capable `tools=` — and `model-not-canonical` — a `model=`
outside the canonical haiku/sonnet/opus vocabulary; a broken
`model_map.json` is the error `model-map-invalid`).

## Workflow design guidelines (for builders)

1. **Single responsibility**: one step = one task one agent can complete.
   Always split at boundaries where the role, mode, or rules change
2. **Model by difficulty, canonical names only**: `haiku` for mechanical
   steps, `sonnet` for standard analysis (and when unsure), `opus` for
   design/diagnosis/review-grade judgment. Never write other model ids —
   binding to actual models is `model_map.json`'s job (see Model resolution)
3. **Role per step**: prefer a named `.claude/agents` definition when a
   suitable one exists (its frontmatter model/tools come along); otherwise
   author a focused inline `<role>` — never force-fit an ill-matched named
   role. Add `mode=` where the processing discipline matters (`execute` for
   strict operations, `survey` for fact collection, `debug` for diagnosis)
4. **File-centric I/O**: hand-offs between steps are "agent writes a file →
   path travels via `output` → next task body embeds `{path}`". Write every
   task body as self-contained, assuming no shared conversation context
5. **Put all required information in the task body**: target table names,
   input/output file paths, formats. References like "the previous step's
   result" are impossible (that context does not exist)
6. **Verify in code**: file existence uses `expect-file=` (on the producing
   step itself); mechanical content checks (line counts, format) use `test=`;
   only semantic checks (presentation, quality) use `ask=`
7. **Choosing on-error**: the default `fail` is safe. Use `retry` for
   idempotent operations with plausible transient failures; reserve `debug`
   for complex steps where diagnosis pays off
8. **Defer what cannot be planned yet**: when the right continuation depends
   on runtime results, insert a `<replan>` node with a precise `<task>` and
   declared `outputs` instead of guessing the step list up front

## Complete examples

A verified minimal example is `../scripts/examples/hello.xml`; a
production-scale example (monthly sales analysis) is
`../scripts/examples/monthly_sales.xml`.

---
This file is the canonical spec. The implementation is `../scripts/wfrun/`
(Python 3.12, standard library only, launched as
`env PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts uv run python -m wfrun`).
Unit tests are in `../scripts/tests/` (`uv run python -m unittest discover -s tests`).
The prompt layer (ERROR:/[BLOCKED: protocols) is probabilistic and outside
unit tests — after editing `modes/*.md` or `guardrails.py`, sample it with
`uv run python ../scripts/evals/prompt_smoke.py` (opt-in; calls the claude CLI).
