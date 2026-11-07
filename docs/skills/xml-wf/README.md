# xml-wf — XML Workflow System v2

Turn a task into a sequence of single-responsibility steps written in XML, and
let a Python runner (`wfrun`) execute them deterministically. Every step runs as
an **isolated `claude -p` subagent** — full context separation, file-based I/O —
optionally under a **role** (WHO the agent is) and an execution **mode**
(HOW it processes).

This documentation lives in `docs/`, outside the skill directory on purpose: it
is reference material for humans and agents, not something to load into context
when the skill runs.

- **README** (this file) — overview and reference for every audience
  (developers, users, humans, LLMs). See also [README_ja.md](README_ja.md).
- **[USER_GUIDE.md](USER_GUIDE.md)** — a practical, step-by-step guide for
  non-developer human users. See also [USER_GUIDE_ja.md](USER_GUIDE_ja.md).
- **[AUTHORING_CONTRACT.md](AUTHORING_CONTRACT.md)** — maintenance obligations
  for skill editors: multi-facet (cc/pi/run-llm) integrity, relationship to
  mode-orchestrator (including the restart gate the two skills align),
  `modes/` propagation pointer.

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
target Claude Code. `run-cc` targets Claude Code. `run-llm` works on Claude Code
and on Pi only: every delegation it makes goes through the host's native
subagent facility (the Agent tool on Claude Code, the `subagent` tool on Pi) and
it starts no `claude` or `pi` process to run a step. On Pi that tool comes from
an extension you set up first (see [Pi setup](#pi-setup)). The one CLI `run-llm`
still starts is `wfrun ask`.

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

## Pi setup

Only `run-llm` on Pi needs this; Claude Code needs none of it. On Pi, `run-llm`
makes every delegation (step, debug diagnosis, `<replan>` builder, decider)
through the native `subagent` tool, which the Pi subagent extension
(`@gigamori/pi-subagent`) provides. A session without that tool (no
`delegation_contract` tool) cannot run it: `run-llm` stops before it creates a
run dir and returns a message that points here. `--run-pi` and `--run-cc` need
no extension.

**What to install.** The `subagent` package of the `pi-extensions` repository,
with its dependency `@gigamori/pi-child-session-core`. Neither is published to
npm (checked 2026-11-06), so the extension goes in as a built copy under the
agent directory's `extensions/`. `<agent dir>` below is `~/.pi/agent`, or
`$PI_CODING_AGENT_DIR` when that is set.

Build a clean checkout of `pi-extensions` (`npm ci`, then `npm run build`) at
the commit `fix(child-session-core): Key top-level package copies by name` or
later. With an older core the copy is registered as `local:dist` and is handed
on to every child session, so a child can delegate again (measured with a stub
provider: 5 model calls with the old core, 3 with the fixed one). Pi Studio
needs the same fix in the core it bundles.

```bash
SRC=<built pi-extensions checkout>
X=<agent dir>/extensions
D="$X/subagent"
C="$D/node_modules/@gigamori/pi-child-session-core"
rm -rf "$D"
mkdir -p "$C"
cp -r "$SRC/packages/child-session-core/dist" "$C/dist"
cp "$SRC/packages/child-session-core/package.json" "$C/package.json"
cp -r "$SRC/packages/subagent/dist" "$D/dist"
cp "$SRC/packages/subagent/package.json" "$D/package.json"
mkdir -p "$X/child-session"
printf '{"explicitModels":"registry"}\n' > "$X/child-session/config.json"
grep -c readDeclaringPackageName "$C/dist/catalog-cli.js"   # expect 2
```

Then restart Pi, or run `/reload` in a running session. On Windows run the
listing in Git Bash.

- The listing copies `package.json` last, so a Pi that starts halfway through
  the install does not see an unfinished package. It copies and does not link,
  so removing the extension later deletes only the copy. The listing starts with
  `rm -rf "$D"`, so running it again replaces an earlier copy instead of nesting
  a second `dist/` inside the old one. That removes only this package's copy,
  not `child-session/`; the `printf` line then rewrites `child-session/config.json`,
  so a rerun drops any `profiles` you added to it.
- `child-session/config.json` is `{"explicitModels":"registry"}`. Without it the
  host refuses an explicit `model` request (`resolve` returns `blocked`:
  "Explicit model channel is not configured."), which is the request `run-llm`
  makes for a tier that has no profile. It sets no `profiles`, so each tier
  (`basic`, `pro`, `ultra`) is `absent` and `run-llm` asks for the model that
  `model_map.json`'s `llm` table gives for it. Adding `profiles` (syntax: the
  `subagent` README, "CLI configuration") makes a configured tier a call-level
  `effort` profile instead, and the host's setting then picks the model. The file
  is shared by every child-session consumer.
- To check the setup, call `delegation_contract` with
  `{"operation":"observe"}` in a Pi session: `routes.native.state` should be
  `available`.
- `available` says that the route exists, not that a model resolves. `resolve`
  also needs each model `run-llm` asks for in the host's catalog, with working
  authentication: with `model_map.json` as shipped, a delegation asks for
  `haiku`, `sonnet` or `opus` (the tiers `basic`, `pro`, `ultra` its lines
  name). A Pi with neither an Anthropic credential nor a provider extension for
  them returns `unavailable` ("No unique compatible binding in the host catalog
  snapshot."), and `run-llm` stops there, before it creates a run dir. Log in to
  Anthropic with `/login`, install such a provider, or set `profiles`. `ask=` is
  outside this check: `wfrun ask` starts Pi with `--model haiku` when the
  workflow reaches it, so it needs `haiku` to work in Pi itself, and `profiles`
  do not cover it.

**What else changes once it is installed.**
- The extension is machine-wide: every Pi process that reads this agent
  directory loads it. That includes the `pi -p` children of `run-pi` and of
  mode-orchestrator's CLI route. `run-pi` does not pass `--no-extensions`
  (model names resolve through extensions), and passes `--tools` only for a step
  that has `tools=`; mode-orchestrator's launch passes neither. A child started
  that way has the `subagent` tool. With the fixed core that child's own
  children do not, so the depth grows by one level at most. This is read from
  the source, not from a run of `pi -p`.
- In a Pi Studio build that bundles the fixed core, a child session had no
  `subagent` tool (observed once). That `run-llm` works in Pi Studio is not
  verified.

To remove it: `rm -rf <agent dir>/extensions/subagent
<agent dir>/extensions/child-session` (look in `child-session/` first for
files you added), then `/reload`. `settings.json` is not touched by the setup.

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
$WFRUN {validate|run|resume|plan|viz|prompt|record|decide|models|poll|stop|judge|dispatch|wait|interp|eval|ask} ...
```

Through the skill, the four modes are selected from natural language or flags:

| Argument | Mode | What it does |
|---|---|---|
| `--build`, a task description, "ワークフロー化" | **Build** | Decompose the task into an approved plan table, then generate and validate the XML. Never executes the task. |
| `--run-cc`, an `.xml` path, "run/execute" | **Run (batch)** | Validate → execute deterministically with `wfrun run`. The default execution mode. |
| `--resume`, a run dir (holding `state.json`) | **Resume** | Continue a failed run from the point of failure. |
| `--run-llm`, "run interactively" | **LLM orchestration** | The agent orchestrates step by step under human supervision, using `wfrun` helper subcommands and file-based exchange. A step's `effort=` is not applied in this mode: the Agent tool takes none, and the `effort` of a Pi `subagent` call selects a tier profile, not the step's attribute. On Pi it needs the subagent extension (see [Pi setup](#pi-setup)). |

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
  executions; runaway protection) are required; `budget-usd`, `decider`,
  `decider-model` optional.
- **`<param>`** — run-time arguments injected via `wfrun run wf.xml -p key=value`
  (`name` required; `required`, `default` optional).
- **`<rules id="…">`** — a prompt fragment injected only into steps whose
  `rules=` attribute references it (`src` optionally points to an external file).
- **`<step>`** — one task run by one agent. `<task>` (required) holds the
  instruction; `<role>` may hold an inline role. Attributes: `id`, `role`,
  `mode`, `model`, `effort`, `output`, `output-type`, `schema`, `rules`,
  `tools`, `expect-file`, `retry`, `decider`, `decider-model`, `timeout`,
  `on-error`.
- **`<replan>`** — defers part of the plan to run time: a builder agent returns a
  continuation workflow that `wfrun` validates and runs inline (one level deep).
- **`<set>`** — assign a variable by interpolation (`value=`) or safe expression
  (`expr=`).
- **Control structures** — `<seq>`, `<if test=|ask=>` with `<then>`/`<else>`,
  `<while test=|ask= max=>`, `<each items=|glob=|range= as=>`, `<parallel>`.

### Roles, modes, models

- **Role** (optional, at most one form): a **named role** (`role="name"`
  resolving to a `.claude/agents/*.md` definition — its body is injected) or an
  **inline `<role>`** child (1–3 sentences authored in place). Named roles bring
  their frontmatter `model`/`tools`; inline and role-less steps should set
  `tools=` explicitly. Omitting the role is a fine default when `mode=`/`rules=`
  already fix the discipline — better than inventing a filler persona.
- **Mode** (`mode=`, optional): a processing discipline injected as `mode:<name>`
  plus its rules. Autonomous modes only: `debug`, `execute`, `plan`,
  `review-doc`, `review-dev`, `review-process`, `survey`, plus aliases
  `verify` → debug and `implement` → execute.
- **Model** (`model=`): a **difficulty class**, not a deployment — canonical
  names are `basic` (mechanical), `pro` (standard, the default), `ultra`
  (design/diagnosis/review). `scripts/wfrun/model_map.json` binds these to the
  actual models at dispatch. The bundled bindings need no configuration to
  start (each tier resolves to a real model out of the box).

Prompt precedence within a step: **Mode > Rules > Task > Role** — or
**Mode > Rules > Task** when the step declares no role.

---

## CLI reference

```
wfrun validate <wf.xml> [--json] [--no-role-check] [--backend auto|cc|pi|llm]
                        [--as-child] [--defined-vars VARS_JSON]
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

`validate --backend llm` is what `run-llm` passes. It checks the XML itself and
starts no CLI: no model name is matched against a catalog, because `run-llm`
resolves models through the orchestrator's own subagent facility.

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
wfrun prompt <wf.xml> <id> --vars V --out PROMPT [--result RESULT] [--fix TEXT] [--new-cycle] [--redispatch JUDGE_REPLY]
wfrun record <wf.xml> <id> --result RESULT --vars V [--log LOG] [--reply LINE] [--answer FILE] [--debug-reply LINE] [--decider human|llm]
wfrun poll   <handle.json> [--wait]     # done(0) / running(10) / deadline-exceeded(11)
wfrun decide <wf.xml> <id> --result RESULT   # delegate an open decision to the llm decider, once
wfrun models <wf.xml>                   # the model requests the delegations will make
wfrun stop   <handle.json> --facet cc --task-stop stopped|completed|failed [--observe 1..60]
                                        # Claude Code only: done(0) / confirmed(12) / unconfirmed(13)
wfrun judge  <wf.xml> <id> --result RESULT   # issue the one judge of an aborted attempt
```

With `--result`, `prompt` starts a new attempt of the step: it gives the attempt
its own result file and its own sentinel, and it refuses (exit 1) when a new
attempt may not start. It also prints `description=wfrun <id> cNN aNN <token>`,
unique to the attempt. On Claude Code, pass it verbatim as the Agent call's
description:
- `record` uses it to find that subagent's transcript, together with every agent the subagent started, and to check them for refused tool calls.
- If it finds a refusal, the step is `error: denied` whatever its result file says, unless the attempt has a recorded stop and its result never completed: `record` then records the attempt `aborted` and prints the check's verdict on a second line, `deny-check: DENIED <n> (details: <transcript>)`.
- If it cannot find the transcript, it still prints its verdict, followed by a second line `deny-check: NO-TRANSCRIPT: <reason>`, and never records the step as clean.
- `steps.log` records the check's result as `deny_check`.

`models` prints the model requests a workflow's delegations will make, as control
facts: a `harness=cc|pi` line (`cc` when `CLAUDE_CODE_SESSION_ID` is set and not
empty), then one line per request with `tier=` (present when the name stands
for `basic`, `pro` or `ultra`), `model=` and `sites=` (the step and `<replan>`
ids, and `<id>:decider` for an llm decider). `prompt`'s dispatch line and
`decide`'s line print the same `tier=` and `model=`, and `decide`'s line also
prints the `timeout=` to give its delegation.

`run-llm` has no layer to choose and no CLI probe: every step, debug
diagnosis, `<replan>` builder and decider runs through the orchestrating
agent's native subagent facility, so it runs on the agent that is
orchestrating, whatever else is installed. On Claude Code that is the Agent
tool. On Pi it is the `subagent` tool: Preparation first checks that the host
offers it (`delegation_contract`) and that every model request of `wfrun
models` resolves, and otherwise stops before it creates a run dir (see [Pi
setup](#pi-setup)). It never falls back to a `claude -p` or `pi -p` process and
never moves to `--run-cc` / `--run-pi` by itself. `ask=` is the one exception:
`wfrun ask` starts a CLI itself, is not a delegation, and works where
delegation does not. On Pi an interrupted step is recorded and the run stops:
nothing observes a `subagent` child's stop, so there is no confirmed stop, judge
or restart, and the run dir cannot be resumed. The options are `--run-pi`,
`--run-cc` or a new run. After a missed deadline on Claude Code, the same
reference gives the stop, judge and restart handling ("On abort").

`dispatch` and `wait` are not part of `run-llm`, and `references/run-llm.md`
does not describe them. They run a step through the claude CLI instead of a
delegation facility: `wfrun` itself calls `claude -p` via a detached wrapper
process.

```
wfrun dispatch <wf.xml> <id> --vars V --run-dir D [--permission-mode M] [--fix TEXT] [--new-cycle]
wfrun wait     <handle.json> --max SEC --vars V [--log LOG]
               # ok(0) / error: <class>(1) / running(10) / aborted(3)
```

After a refused tool call, `wait` prints `error: denied (details: <file>)`.
After any failed attempt, `dispatch` applies the same retry/debug handling as
`run` and refuses a re-run that handling would not make (a class never
retried such as `denied`, a spent `retry`, a second fixed re-run); the refusal
says what to do instead. `--fix` is accepted only right after `wait` printed
`debug: RETRY`. Pass `--new-cycle` only for a new `<while>`/`<each>` iteration.

```
wfrun interp <text> --vars V            # interpolate {var} references
wfrun eval   <expr> --vars V            # evaluate a test= expression → true/false
wfrun ask    <question> [--vars V] [--model basic] [--backend auto|cc|pi] [--quiet] [--log LOG]
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
└── steps/<id>_<attempt n>/{system.md, prompt.md, result.json, stream.jsonl, stderr.log}
```

`wfrun resume runs/<dir>` replays the recorded successes (reusing step results
and `ask=` judgments without re-running) and starts actual execution at the
first mismatch or unrecorded point. To fix a failing step, edit `workflow.xml`
inside the run dir before resuming — do not change already-succeeded definitions.

This layout is a batch run's. A `run-llm` run keeps its files in
`<ws>/tmp/runs/<name>_<ts>-llm/` instead, where `<ws>` is the directory the
session was started in: `vars.json`, `steps.log`, `steps/<id>_cNN_aNN_*` (one
set of files per attempt), `replans/`, `decisions/`, and `worktrees.json` when the
run creates a git worktree. It has no `state.json` or `events.jsonl`, it cannot
be resumed with `wfrun resume`, and the run never deletes it.

---

## Error handling

Detection priority:
1. A refused tool call (`denied`): listed in `permission_denials` on any result event, including a refusal inside an agent the step started.
2. Non-zero exit or `is_error`.
3. Timeout.
4. The response body starts with `ERROR:`.
5. The first line starts with `[BLOCKED:` (a mode/rules refusal).
6. The body carries a `DECISION:` request, which is not an error (see below).
7. `schema` was given but no structured output came back.
8. A result that would pass carries no `permission_denials` field (`unchecked`: the refusal check could not run).
9. An `expect-file` path is missing.

`result.json` holds the last result event, and `stream.jsonl` holds the full
stream: the refusal behind a `denied` step can sit on an earlier event.
`denied` and `unchecked` are never retried and never sent to debug.

Handling: deterministic **`retry`** (identical prompt) → then `on-error`:
`fail` (stop; resume is the recovery path), `ignore` (record and continue), or
`debug` (the debug role diagnoses via `{action: RETRY|FAIL, reason,
fix_instruction?}`; RETRY re-runs exactly once with the fix appended).

Give every file-producing step an `expect-file=`: the `ERROR:`/`[BLOCKED:`
protocols catch only *compliant* refusals, while `expect-file` verifies the
deliverable itself.

**Decision requests.** A step that hits a genuinely ambiguous fork raises
`DECISION:` (options, recommendation, work state) instead of guessing. Under
the default `decider="human"` the run stops (`awaiting-decision`, exit 4) and
`wfrun resume <dir> --answer <step>=<file>` continues it; with
`decider="llm"` an adjudicator model (`decider-model`, default `ultra`)
settles the fork in-process — capped at 2 rulings per step visit, with
irreversible / outward-facing / goal-changing forks escalated to a human.
Works on both backends (schema-forced ruling on claude, text ruling on pi).
See `references/spec.md`, "Decision requests".

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
