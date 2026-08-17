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

A step may declare a **role** (WHO the agent is) and an execution **mode** (HOW
it processes; a bundled snapshot of the role-mode prompt set). Both are
optional and both are injected into the step prompt by wfrun — `--agent` is
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
| `budget-usd` | - | Cumulative cost ceiling (USD). When exceeded, execution stops before the next step starts. Adjudication calls under `decider="llm"` count toward it |
| `decider` | - | Who settles a `DECISION:` request a step raises (see "Decision requests"). Default `human`: the run stops and a person answers via `resume --answer`. `llm`: an adjudicator model settles it in-process and the run continues — capped at 2 rulings per step visit, and escalated forks still stop for a human. A step-level `decider=` overrides this |
| `decider-model` | - | Model for the `llm` adjudicator (default `opus`; a canonical class, or any name the runner's model table accepts). Step attribute > workflow attribute > default |

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
| `role` | - | - | Named role: a `.claude/agents/*.md` definition (project first, then the user agents dir — `$CLAUDE_CONFIG_DIR` or `~/.claude` — `/agents/`) whose **body is injected** as the `<role>` block. At most one of `role=` or an inline `<role>` child; declaring neither runs the step role-less |
| `mode` | - | - | Execution mode (see "Execution modes" below) |
| `model` | - | role frontmatter | Canonical difficulty name — `haiku`/`sonnet`/`opus` only (step attribute wins over the named role's frontmatter). Bound to an actual model per runner at dispatch (see "Model resolution"); other strings pass through, and warn (`model-not-canonical`) under `--backend pi`, where the resolved name is also checked against pi's live catalog |
| `effort` | - | - | `low`…`max` (forwarded to `--effort`) |
| `output` | - | - | Variable name that receives the result |
| `output-type` | - | `file` | See below |
| `schema` | - | - | JSON Schema (inline, or `@path` for a file reference). Forwarded to `--json-schema`; forces structured output. **run-cc only** — the pi backend has no equivalent and refuses such a workflow at startup (`references/run-pi.md`, "Replacing `schema=`") |
| `rules` | - | - | Comma-separated rules ids to inject |
| `tools` | - | role frontmatter | Forwarded to `--allowedTools` (e.g. `"Read,Write"`; step attribute wins). Names are Claude Code's; run-pi translates them to pi's (`Glob` → `find`, the rest lowercased) and **stops the step** on a name it cannot map (`MultiEdit`, `NotebookEdit`, `Task`, `Agent`, typos) rather than dropping it silently |
| `expect-file` | - | - | Comma-separated paths (`{var}`-interpolated; relative to the XML dir) that must exist after the step. Missing = step failure (retry / on-error apply). The deterministic deliverable check — a compliant-looking response without the artifact is caught |
| `retry` | - | `0` | Deterministic retry count (re-run with the identical prompt) |
| `decider` | - | workflow attr | Per-step override of the workflow's `decider=` (see the root table and "Decision requests") |
| `decider-model` | - | workflow attr | Per-step override of the adjudicator model. The adjudication call also uses this step's `timeout` |
| `timeout` | - | `600` | Seconds. Process is killed on overrun → error |
| `on-error` | - | `fail` | `fail` (stop immediately) / `ignore` (record and continue) / `debug` (debug-role diagnosis). **`debug` is run-cc only** — it is built on the claude CLI's structured output and Claude Code's config tree, so the pi backend refuses such a workflow at startup (`references/run-pi.md`, "Replacing `on-error=\"debug\"`") |

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
child is a parse error.

**Role is optional.** A step that declares neither form runs role-less: no
`<role>` block is injected and the framework header drops its Role axis (see
"Execution modes"). Prefer that over inventing a generic persona — when
`mode=` and `rules=` already fix the discipline, a "You are a careful
engineer" preamble costs tokens and steers almost nothing. Note that a
role-less step has no frontmatter to inherit `model`/`tools` from, so those
come only from the step attributes; the `tools-not-inherited` lint warning
flags the ones that set neither.

An empty `role=""` attribute is accepted as an **explicit** role-less
declaration, equivalent to omitting `role=` entirely — useful for a
programmatically generated step that always emits the attribute. It is not a
parse error.

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
The builder's role follows the same contract as `<step>`: at most one of
`role=` or an inline `<role>` child, and neither is fine. There is no `mode=`
(the builder prompt is a fixed XML-only contract that a mode would interfere
with). Because a replan has no mode, it also gets no framework header and no
`_common.md` — so a role-less replan sends an empty system channel, and the
backend omits the flag entirely.

| Attribute | Required | Default | Meaning |
|---|---|---|---|
| `id` | ✔ | - | Unique identifier (shares the step id namespace) |
| `role` | - | - | Builder role that generates the continuation (or an inline `<role>` child); omit to run role-less |
| `model` / `effort` | - | role frontmatter | Forwarded like `<step>` |
| `max-steps` | - | `20` | Cap on the continuation: its `max` must not exceed this, and its executed steps are additionally capped here |
| `outputs` | - | - | Comma-separated variable names the continuation must define (checked after it runs; missing = failure) |
| `retry` | - | `0` | Regeneration attempts when the produced XML fails validation (validator errors are fed back to the builder) |
| `timeout` | - | `600` | Seconds for the builder call |
| `on-error` | - | `fail` | `fail` / `ignore` (a failed replan leaves `outputs` unset). `debug` is not meaningful here |

Semantics:
- The builder runs with read-only tools (`Read,Glob,Grep`), receives the spec
  path, the list of available named roles, the execution-mode vocabulary
  (aliases excluded; with one-line usage guidance — continuation steps are
  ordinary `<step>`s and may set `mode=`), and the variable table, and must
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
declaration, the mode body, and the all-modes rules (`_common.md`); a
framework header is injected for every step, mode or not.

The header declares the prompt axes in tag vocabulary and their precedence. It
comes in two variants, picked by whether the step declares a role —
`_meta_role.md` for a step that does:

> **Mode > Rules > Task > Role** — constraints (mode, rules) over the
> instruction (task) over the persona (role). If a mode or rules constraint
> truly blocks the task, the agent replies with a single
> `[BLOCKED: mode-rule <name>]` line and stops (a detected error, see below).
> Files at paths the task names are the step's own mode-output — writing them
> is always allowed, whatever the mode.

and `_meta.md`, the same document with the Role axis dropped
(**Mode > Rules > Task**), for a role-less one. Both are xml-wf's own, not
copies of the plugin's same-named files: a step also carries `<rules>` and a
`<task>`, which the plugin's axis model has no notion of.

Available modes (**autonomous only** — the plugin's interactive modes
ask/brainstorm/discuss/organize need a live human exchange and are not
bundled): `debug`, `execute`, `plan`, `review`, `review-dev`, `survey` —
plus the aliases `verify` → debug and `implement` → execute (the alias picks
the file; the declared name is preserved in the prompt). Unknown names are a
validate error (`mode-unknown`).

Practical guidance: `execute` suits strict do-exactly-this steps (operations,
file writes), `survey` suits fact-collection steps, `debug` suits diagnosis
steps. Steps without `mode=` get no mode rules — only the framework header and
the `<role>` block (if any).

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
  framework header                 ← always; _meta_role.md with a role,
                                     _meta.md (Role axis dropped) without one
  <role>...</role>                 ← named definition's body, or the inline
                                     <role>; omitted when neither is declared
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
  run-cc steps, debug diagnoses, replan builders, and `ask=` when its
  `--backend` resolves to `cc` (the default `auto` does this whenever
  `CLAUDE_CODE_SESSION_ID` is set) — and must hold claude CLI model names
- the `llm` table covers run-llm step delegation (`wfrun prompt` prints the
  resolved name on the dispatch line, as `model=X (mapped from Y)` when a
  mapping applied, and the orchestrator passes it through verbatim), **run-pi
  steps** and `ask=` when `--backend` resolves to `pi` — all read names meant
  for "the orchestrator's own execution facility", not the claude CLI, so `pi`
  canonical names live in this table, not `cc`'s. **Which facility that is
  depends on the harness**: the table has one set of values, so a map
  hand-edited for one orchestrator's names will not resolve under another.
  The bundled identity map avoids the problem entirely — the canonical names
  resolve as-is on both the claude CLI and pi

The bundled map is the identity (zero-config = current behavior). Unmapped
names pass through. Applied mappings are recorded as `model-map` events;
a broken map file is a startup error, never silently ignored. Resume note:
replayed steps are untouched, but steps that actually re-run resolve against
the map as it is at resume time.

### Error detection (priority order)

Each check below assigns an `error_class` (`claude_cli.classify_result()`),
which is what retry/debug policy (next section) actually keys on — not
string-matching `error`.

1. `permission_denials` present in the result JSON → `denied`. Checked
   first and independent of `is_error` (observed with `is_error:false`,
   exit 0)
2. `claude` result JSON reports `is_error` — the exit code is not checked
   first: a non-zero exit can still carry a fully-formed error JSON on
   stdout (e.g. an unknown `--model` exits 1 with a JSON body), so JSON
   parsing is attempted before the exit code is consulted; only an
   unparseable stdout counts as a launch failure (`env`). Do not classify
   on the result JSON's `subtype` field — it is not a reliable
   success/error signal (`is_error:true` has been observed together with
   `subtype:"success"`); classify on `terminal_reason` / `api_error_status`
   instead: `terminal_reason:"api_error"` with a retry-safe HTTP status
   (429/5xx) → `transient`; with any other status (including a
   missing/unrecognized one — fail-closed) → `env`; any other
   `terminal_reason` → `behavioral`
3. `timeout` exceeded (SIGKILL) → `timeout`
4. Response body starts with `ERROR:` (guardrail protocol) → `guardrail`
5. Response body's first line starts with `[BLOCKED:` (mode/rules refusal —
   the line is recorded as the error reason) → `refusal`
6. Response body opens with `DECISION:` — **or** contains, below preamble
   prose, a line-anchored `DECISION:` line whose tail parses as a complete
   5-field payload (a measured model behaviour; a mere mention never
   matches) → `decision`. **Not a failure**: a well-formed request stops
   the run for adjudication instead of entering retry/on-error (see
   "Decision requests" below); only a malformed payload fails the run
7. Response body is empty (after stripping the `[Mode: x]` line) and no
   structured output came back → `behavioral`
8. `schema` was given but no structured output came back → `behavioral`
9. A path named in `expect-file` does not exist after the response
   (checked by the executor, not `classify_result()`) → `behavioral`

Items 4–6 are token-prefix protocols and catch only *compliant* reports (an
agent that narrates before the marker, or half-works then apologizes,
classifies as success — with the one payload-parse exception item 6 states).
They are likelihood levers, not gates — the deterministic layer is items 8–9
plus downstream `test=` checks; give every file-producing step an
`expect-file`. A *successful* response that carries a stray line-anchored
`ERROR:`/`[BLOCKED:`/`DECISION:` token is reported as a run warning rather
than silently passing.

### Error handling (modernized ADP)

This table and the executor logic behind it (`claude_cli.is_retryable()` /
`is_debuggable()`) are shared verbatim by run-llm's layer A (`wfrun
dispatch`/`wait`): the wrapper process calls `classify_result()` directly,
so an A-layer step is retried/debugged under exactly this policy, not a
separate one. Layer B (`wfrun record`) has its own, text-based decision
table instead (ok/error/aborted, no `error_class`) — see `run-llm.md`.

Retry and debug are gated on `error_class`, not on error text:

| error_class | deterministic retry | `on-error="debug"` |
|---|---|---|
| `timeout`, `behavioral` | consumed | eligible |
| `transient` | consumed | **not eligible** |
| `guardrail` | skipped (identical prompt hits the same guardrail) | eligible |
| `env` | skipped | eligible |
| `refusal`, `denied` | skipped | **not eligible** |

`refusal`/`denied` are the step agent's or the permission system's final
word on this exact request — not a bug a debug re-diagnosis could fix.
`transient` is retried (an upstream hiccup can clear on its own) but is
*never* handed to debug even when retries are exhausted: classifying a
transient API error as a fixable "failed" and routing it into the debug/
recovery loop is the exact misclassification that produced a retry-storm
incident this design responds to (a permission-denied `bash` tool call,
misclassified as fixable, spawned dozens of child sessions per minute).

1. **Deterministic retry**: re-run `retry` times with the identical prompt
   (absorbs transient failures), skipped for `error_class` in
   `{env, guardrail, refusal, denied}`
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

### Decision requests (`DECISION:`)

A step that reaches a fork it may not settle alone — two defensible readings,
or the task simply silent — must not quietly pick one. Every step prompt
carries a protocol (guardrail rule 5) for declaring it:

```
DECISION: <one-line summary of the fork>
fork: <what is ambiguous>
options:
  1. <option> -- <the cost of choosing it wrongly>
  2. <option> -- <the cost of choosing it wrongly>
recommendation: <option number | none>
work-state: <complete|stopped>
output: <the value that becomes this step's output>   (complete only)
```

A well-formed request is **not an error** — it never enters retry, `on-error`,
or debug. What happens next is the `decider=` attribute's call:

- **`human` (default)**: the run stops (`awaiting-decision`, exit 4) and the
  report prints the request path, its numbered options, and the exact
  `wfrun resume <dir> --answer <step>=<file>` command. The answer file's first
  line is `option: <N|none>`; the rest is free text (required with `none`).
- **`llm`**: an adjudicator model (`decider-model`, resolved per backend) is
  called in-process and the run continues on its ruling — no stop. Bounds:
  at most **2 llm rulings per step visit** (the third stops for a human, and
  human answers never consume the cap); forks that are irreversible,
  outward-facing, or change the workflow's goal are **escalated** to a human
  rather than settled (the run stops, the answer path stays empty, and the
  reason is recorded); any unusable ruling likewise falls back to a human.
  Adjudication cost joins `cost_usd` / `budget-usd`. On run-cc the ruling is
  schema-forced; on run-pi the adjudicator writes the answer format itself —
  either way one shared parser validates it, exactly as it validates a
  human's file.

How an answered fork continues: if the step declared its deliverable already
written (`work-state: complete`, `expect-file` verified, and the answer picked
the recommended option), the value is adopted **without re-running the step** —
form (a). A step that declares an `output` variable has two more conditions to
meet: the payload must carry the `output:` value, and the variable must be
`output-type="file"`. A value-typed variable would take that line verbatim as
the step's output, and it was written before the ruling existed — so a
value-typed step re-runs and produces the value itself. Anything else re-runs the step
once with every settled ruling of that visit injected into its prompt — form
(b) — and the report says why (`b_reason`). A malformed payload is the one
genuine failure: the run stops FAILED with the payload path for a human to
read, and no retry or debug fires.

Procedures live per backend: `run-cc.md` ("On decision"), `run-pi.md`
(`decider="llm"` works here), `run-llm.md` ("On decision" — the orchestrator
delegates the ruling and never reads the request).

### Run dir and resume

```
runs/<name>_<YYYYMMDD-HHMMSS>/
├── workflow.xml        # snapshot taken at run time (resume reads this)
├── params.json
├── state.json          # {status, vars, step_count, cost_usd, error}; status adds awaiting-decision
├── events.jsonl        # append-only log (step/cond/test/set/debug/replan/run/decision/answer)
├── outputs/<id>.md     # response bodies for output-type=file
├── replans/<id>_<nn>.xml  # validated replan continuations
├── decisions/          # DECISION: ledger: <id>_cNN_dNN_request.md + _answer.md
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
wfrun validate <wf.xml> [--json] [--no-role-check] [--backend auto|cc|pi]
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
- That directory must not be inside Claude's config tree — `~/.claude`, or the
  `$CLAUDE_CONFIG_DIR` dir when set (both protected) — the CLI demands
  interactive write approval under its own config tree, so file-writing steps
  would fail; `wfrun run`/`resume` reject this at startup — copy bundled
  examples to a normal project directory before running them
- The helper subcommands `interp` / `eval` / `ask` / `prompt` / `record` /
  `poll` (layer B) and `dispatch` / `wait` (layer A) exist for
  LLM-orchestrated execution; see `references/run-llm.md`
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
undersized `max`, `tools-not-inherited` — a step with no named `role=` to
inherit tools from and no `tools=` of its own runs with the CLI's default
tool permissions — and
`mode-write-tools` — a non-writing mode (survey/plan/review/review-dev)
combined with write-capable `tools=`; a broken `model_map.json` is the error
`model-map-invalid`).

Model names are checked **only under `--backend pi`** (default `auto`, resolved
from `CLAUDE_CODE_SESSION_ID` exactly as `run` and `ask` resolve it). There,
every `model=` and every adjudicator model an `llm` decider would actually be
sent — resolved through the `llm` table first — is matched against
`pi --list-models`: a name matching nothing is the error
`pi-model-unavailable`, and a `model=` outside the canonical vocabulary is the
warning `model-not-canonical`. If the catalog cannot be read the warning is
`pi-model-unverified`, never a pass. Under `cc` no model name is checked at
all: those runs stay inside the canonical vocabulary, which `model_map.json`
binds to claude CLI names, so there is no catalog for a name to be missing
from.

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
