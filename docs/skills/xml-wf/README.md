# xml-wf — XML Workflow System v2

Turn a task into a sequence of single-responsibility steps written in XML, and
let a Python runner (`wfrun`) execute them deterministically. Every step runs as
an **isolated `claude -p` subagent** — full context separation, file-based I/O —
under an explicit **role** (WHO the agent is) and an optional execution **mode**
(HOW it processes).

This documentation lives in `docs/`, outside the skill directory on purpose: it
is reference material for humans and agents, not something to load into context
when the skill runs.

- **README** (this file) — overview and reference for every audience
  (developers, users, humans, LLMs). See also [README_ja.md](README_ja.md).
- **[USER_GUIDE.md](USER_GUIDE.md)** — a practical, step-by-step guide for
  non-developer human users. See also [USER_GUIDE_ja.md](USER_GUIDE_ja.md).

The canonical technical spec is the skill's `references/spec.md`. This README
summarizes it; the spec governs when they disagree.

---

## The core idea

The orchestrator is **Python, not an LLM**. The XML is not read by a language
model — `wfrun` parses it and walks the control flow deterministically. An LLM
is involved in exactly four places:

1. **Step execution** — one independent `claude -p` subprocess per `<step>`.
2. **LLM condition judgment** — the `ask=` attribute (structured output forces a
   boolean + reason).
3. **Failure diagnosis** — the debug role on `on-error="debug"` (optional).
4. **Dynamic replanning** — a builder role generating a continuation workflow at
   a `<replan>` node (optional, one level deep).

Design principles: **determinism** (same XML + same inputs → same path),
**context separation** (steps share no conversation; hand-offs are variables and
files only), **file-centric I/O** (large data moves as file paths),
**verifiability** (closed schema; `wfrun validate` checks statically before
execution), and **auditability** (every prompt, response, and judgment is
recorded under `runs/`, and a run can resume from the point of failure).

---

## Requirements

- **Python 3.12+**
- **uv** (all Python is launched via `uv run`)
- **At least one agent CLI**, and which one decides what the workflow may use:
  - **claude CLI v2.1.214+** — required by the `cc` backend (every step, debug
    diagnosis and replan builder runs as `claude -p` / `--json-schema`; role
    definitions are injected into prompts, `--agent` is not used)
  - **pi CLI** — enough on its own for the `pi` backend, which needs no claude
    install. It refuses `schema=` and `on-error="debug"` at startup because pi
    cannot enforce them (see `references/run-pi.md` for the rewrites)
  - `ask=` judgments follow the same auto-detection as `run --backend`

The runner uses the standard library only. `build` mode invokes no CLI — the
session LLM authors the XML and finishes at `wfrun validate` — though its role
discovery (`.claude/agents`, `$CLAUDE_CONFIG_DIR`) and `tools=` vocabulary
target Claude Code. `run-cc` targets Claude Code; the `run-llm` protocol works
on any agent platform with a subagent facility.

**Multiple `claude` installs (Windows):** wfrun resolves the actual `claude`
executable itself rather than launching whatever `PATH` returns verbatim —
launching the npm `.cmd`/`.bat` launcher directly (without a shell) either
fails to start or corrupts prompts containing `& | % ^ < >` or newlines,
since Windows will not resolve `claude` to `claude.cmd` without a shell, and
that shim's own `cmd.exe` layer mangles such argv. There is no dedicated
environment variable for this: if more than one `claude` is installed,
**the one that appears earlier on `PATH` wins**. Reorder `PATH` to choose
between them.

---

## Skill layout

```
xml-wf/                        # the skill
├── SKILL.md                   # mode dispatch + principles
├── references/
│   ├── spec.md                # canonical control-structure spec (v2)
│   ├── build.md               # Build mode procedure
│   ├── run-cc.md              # Run (batch) + Resume procedure
│   └── run-llm.md             # LLM-orchestration procedure
└── scripts/
    ├── wfrun/                 # the runner (Python 3.12, stdlib only)
    │   ├── __main__.py        # CLI entry
    │   ├── modes/             # bundled role-mode prompts (execute, survey, …)
    │   └── model_map.json     # difficulty-name → real-model binding
    ├── examples/
    │   ├── hello.xml          # minimal verified example
    │   ├── monthly_sales.xml  # production-scale example
    │   ├── rules/             # external rules referenced by examples
    │   └── .claude/agents/    # roles the examples resolve (writer, debug)
    ├── tests/                 # unit tests (unittest)
    └── evals/prompt_smoke.py  # opt-in prompt-protocol sampler (calls the CLI)
```

---

## Invocation

Always launch the runner through this wrapper (`${CLAUDE_SKILL_DIR}` resolves to
the skill's directory on Claude Code):

```bash
WFRUN="env PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts uv run python -m wfrun"
$WFRUN {validate|run|resume|plan|viz|prompt|record|poll|dispatch|wait|interp|eval|ask} ...
```

Through the skill, the four modes are selected from natural language or flags:

| Argument | Mode | What it does |
|---|---|---|
| `--build`, a task description, "ワークフロー化" | **Build** | Decompose the task into an approved plan table, then generate and validate the XML. Never executes the task. |
| `--run-cc`, an `.xml` path, "run/execute" | **Run (batch)** | Validate → execute deterministically with `wfrun run`. The default execution mode. |
| `--resume`, a run dir (holding `state.json`) | **Resume** | Continue a failed run from the point of failure. |
| `--run-llm`, "run interactively" | **LLM orchestration** | The agent orchestrates step by step under human supervision, using `wfrun` helper subcommands and file-based exchange. |

---

## XML v2 at a glance

A minimal, verified example (`scripts/examples/hello.xml`):

```xml
<workflow name="hello" version="2" max="10" budget-usd="1.0">
  <param name="topic" default="the sea"/>
  <rules id="style">Write concisely. The poem must be 3 lines or fewer.</rules>

  <step id="s1_write" role="writer" mode="execute" rules="style"
        output="poem_path" output-type="value">
    <task>Write a short poem about {topic} and save it to the file
output/poem.txt. Return only the relative path of the saved file.</task>
  </step>

  <step id="s2_count" role="writer" output="line_count" output-type="value"
        schema='{"type":"object","properties":{"line_count":{"type":"integer"}},"required":["line_count"]}'>
    <task>Read the file {poem_path} and return its line count.</task>
  </step>

  <if ask="Does the content of file {poem_path} read as a poem?">
    <then>
      <step id="s3_note" mode="execute" tools="Write">
        <role>You are a careful file clerk who writes exactly what is
requested and never editorializes.</role>
        <task>Write exactly APPROVED to ./output/note.txt. Return only the
file path.</task>
      </step>
    </then>
  </if>
</workflow>
```

### Elements

- **`<workflow>`** (root) — `name`, `version="2"`, `max` (cap on total step
  executions; runaway protection) are required; `budget-usd` optional.
- **`<param>`** — run-time arguments injected via `wfrun run wf.xml -p key=value`
  (`name` required; `required`, `default` optional).
- **`<rules id="…">`** — a prompt fragment injected only into steps whose
  `rules=` attribute references it (`src` optionally points to an external file).
- **`<step>`** — one task run by one agent. `<task>` (required) holds the
  instruction; `<role>` may hold an inline role. Attributes: `id`, `role`,
  `mode`, `model`, `effort`, `output`, `output-type`, `schema`, `rules`,
  `tools`, `expect-file`, `retry`, `timeout`, `on-error`.
- **`<replan>`** — defers part of the plan to run time: a builder agent returns a
  continuation workflow that `wfrun` validates and runs inline (one level deep).
- **`<set>`** — assign a variable by interpolation (`value=`) or safe expression
  (`expr=`).
- **Control structures** — `<seq>`, `<if test=|ask=>` with `<then>`/`<else>`,
  `<while test=|ask= max=>`, `<each items=|glob=|range= as=>`, `<parallel>`.

### Roles, modes, models

- **Role** (required per step, exactly one form): a **named role** (`role="name"`
  resolving to a `.claude/agents/*.md` definition — its body is injected) or an
  **inline `<role>`** child (1–3 sentences authored in place). Named roles bring
  their frontmatter `model`/`tools`; inline roles should set `tools=` explicitly.
- **Mode** (`mode=`, optional): a processing discipline injected as `mode:<name>`
  plus its rules. Autonomous modes only: `debug`, `execute`, `plan`, `review`,
  `review-dev`, `survey`, plus aliases `verify` → debug and
  `implement` → execute.
- **Model** (`model=`): a **difficulty class**, not a deployment — canonical
  names are `haiku` (mechanical), `sonnet` (standard, the default), `opus`
  (design/diagnosis/review). `scripts/wfrun/model_map.json` binds these to the
  actual models at dispatch. The bundled map is the identity (zero-config).

Prompt precedence within a step: **Mode > Rules > Task > Role**.

---

## CLI reference

```
wfrun validate <wf.xml> [--json] [--no-role-check] [--as-child] [--defined-vars VARS_JSON]
wfrun run      <wf.xml> [-p k=v ...] [--run-dir D] [--runs-root runs] [--permission-mode acceptEdits] [--backend auto|cc|pi] [--inherit-model M]
wfrun resume   <run_dir> [--base-dir D] [--permission-mode ...]
wfrun plan     <wf.xml>                 # print the step tree (no execution)
wfrun viz      <wf.xml> [--out FILE]    # mermaid flowchart of the control flow
```

`--backend` on `run` picks which CLI executes the steps: `cc` (the claude CLI)
or `pi`. The default `auto` reads `CLAUDE_CODE_SESSION_ID` — set → `cc`, unset
→ `pi` — so the choice is made in code rather than left to the caller to
remember. The backends are not interchangeable: the pi backend refuses
`schema=` and `on-error="debug"` before any process starts, because it cannot
enforce either, and `references/run-pi.md` gives the rewrites plus the
behaviour that differs (doubled retries, no `transient` class, `budget-usd`
mostly inert, translated `tools=` names, and argument specifiers such as
`Bash(git:*)` widened to the whole tool because pi has no per-command
matching). `resume` takes the backend recorded in the run dir and never
re-detects, so one run never spans two CLIs.

`--inherit-model` supplies the model for steps that declare none of their own
(no `model=` attribute, no role-frontmatter default). wfrun runs as its own
process and cannot detect the model of the session that invoked it, so the
invoking skill passes it explicitly; the value is a concrete model identifier
used as-is, not a canonical difficulty class resolved through
`model_map.json`. Omitting it is not an error — such steps are then left to
whatever model the backend CLI picks for itself, which is per-machine,
undocumented, and not necessarily one the CLI calls a default (under pi,
measured selecting an enabled provider over pi's own `defaultModel`, and
unrelated to the calling session); a `note:` line at run start names every
step that would be affected. `resume` inherits the value recorded in the run dir the
same way it inherits the backend, and takes no override flag of its own.

Helper subcommands for `run-llm` orchestration (task content never passes
through the caller):

```
wfrun prompt <wf.xml> <id> --vars V --out PROMPT [--result RESULT] [--fix TEXT] [--attempt N]
wfrun record <wf.xml> <id> --result RESULT --vars V [--log LOG] [--reply LINE]
wfrun poll   <handle.json>              # layer B: done(0) / running(10) / deadline-exceeded(11)
```

The layer decides **who executes the steps**: layer A runs them through the
claude CLI, layer B through the orchestrating agent's own subagent facility.
The probe only checks whether the claude CLI exists, so on a non-Claude-Code
harness that also has claude installed it picks A — select B explicitly when
the steps are meant to run on that harness. Layer B also works on a harness
with no subagent tool at all (Pi); `references/run-llm.md` gives the delivery
shape.

Layer A (environments where `claude --version` succeeds: no subagent
delegation -- `wfrun` itself calls `claude -p` via a detached wrapper
process. See `references/run-llm.md` for the full protocol):

```
wfrun dispatch <wf.xml> <id> --vars V --run-dir D [--permission-mode M] [--fix TEXT] [--new-cycle]
wfrun wait     <handle.json> --max SEC --vars V [--log LOG]
               # ok(0) / error: <class>(1) / running(10) / aborted(3)
```

```
wfrun interp <text> --vars V            # interpolate {var} references
wfrun eval   <expr> --vars V            # evaluate a test= expression → true/false
wfrun ask    <question> [--vars V] [--model haiku] [--backend auto|cc|pi] [--quiet] [--log LOG]
```

`--backend` (default `auto`): dispatches the judgment through the `claude` CLI
(`cc`) or the `pi` CLI (`pi`). `auto` detects the running harness from
`CLAUDE_CODE_SESSION_ID` (set → `cc`, unset → `pi`) — it does not inspect
which binaries happen to be on `PATH`. The Pi backend has no forced
structured-output flag (prompted instead, with a two-pass JSON parse) and
never reports `cost_usd` (always `0.0`); `--log` entries record which
backend ran under `"backend"`.

Notes:
- `--permission-mode` is forwarded only to steps whose resolved tools can write.
  Give survey/review steps a read-only `tools=` so the widened permission never
  reaches them.
- Named-role resolution, rules-relative paths, and the subprocess cwd are all
  based on **the directory containing the XML file**. That directory must not be
  inside Claude's config tree (`~/.claude`, or the `$CLAUDE_CONFIG_DIR` dir
  when set — both are protected).

---

## Run directory and resume

```
runs/<name>_<YYYYMMDD-HHMMSS>/
├── workflow.xml     # snapshot taken at run time (resume reads this)
├── params.json
├── state.json       # {status, vars, step_count, cost_usd, error}
├── events.jsonl     # append-only execution log
├── outputs/<id>.md  # response bodies for output-type=file
├── replans/<id>_<nn>.xml
└── steps/<id>_<attempt n>/{system.md, prompt.md, result.json, stderr.log}
```

`wfrun resume runs/<dir>` replays the recorded successes (reusing step results
and `ask=` judgments without re-running) and starts actual execution at the
first mismatch or unrecorded point. To fix a failing step, edit `workflow.xml`
inside the run dir before resuming — do not change already-succeeded definitions.

---

## Error handling

Detection priority: non-zero exit / `is_error` → timeout → response body starts
with `ERROR:` → first line starts with `[BLOCKED:` (mode/rules refusal) →
`schema` given but no structured output → an `expect-file` path is missing.

Handling: deterministic **`retry`** (identical prompt) → then `on-error`:
`fail` (stop; resume is the recovery path), `ignore` (record and continue), or
`debug` (the debug role diagnoses via `{action: RETRY|FAIL, reason,
fix_instruction?}`; RETRY re-runs exactly once with the fix appended).

Give every file-producing step an `expect-file=`: the `ERROR:`/`[BLOCKED:`
protocols catch only *compliant* refusals, while `expect-file` verifies the
deliverable itself.

---

## Examples

- **`scripts/examples/hello.xml`** — minimal, self-contained. Its roles
  (`writer`, `debug`) are bundled under `scripts/examples/.claude/agents/`, so it
  validates and runs from the `examples/` directory as-is.
- **`scripts/examples/monthly_sales.xml`** — production-scale (monthly sales
  analysis). It expects the roles `analytic-sql-coder`, `faithful-operator`, and
  `data-explainer` to be defined under `.claude/agents/` in the project where you
  run it; those role definitions are **not bundled**. Validate it with
  `--no-role-check`, or supply the roles, before running.

---

## Development

```bash
# from scripts/
uv run python -m unittest discover -s tests      # deterministic unit tests
uv run python -m wfrun validate examples/hello.xml
uv run python evals/prompt_smoke.py              # opt-in; samples the prompt layer, calls the CLI
```

The prompt layer (the `ERROR:`/`[BLOCKED:` protocols) is probabilistic and
outside the unit tests. After editing `modes/*.md` or the prompt assembly,
sample it with `prompt_smoke.py` and compare compliance rates before/after.
