---
name: xml-wf
description: "Build, run, and resume XML v2 workflows end to end. Use when the user wants to turn a task into an XML workflow (build, ワークフロー化), execute an existing workflow XML (--run-cc, 実行), resume a failed run (--resume, 再開), or have the LLM orchestrate step-by-step under supervision (--run-llm, 対話的に実行). The runner (wfrun) and the spec are bundled inside this skill."
argument-hint: "[--build|--run-cc|--run-pi|--run-llm|--resume] [task description|xml path|run dir]"
compatibility: "Requires Python 3.12+ and at least one agent CLI. Batch execution has two backends, selected by `wfrun run --backend` (default auto, from CLAUDE_CODE_SESSION_ID): run-cc needs the claude CLI v2.1.214+ (every step, debug diagnosis and replan builder is an isolated claude -p call; --json-schema; roles are injected into prompts, --agent is not used), run-pi needs only the pi CLI and refuses schema= and on-error=debug at startup because pi cannot enforce them -- references/run-pi.md gives the rewrites. ask= judgments follow the same auto-detection. Build mode invokes no CLI (the session LLM authors the XML, ending at wfrun validate) but targets Claude Code's namespaces: roles are collected from its config tree (.claude/agents, $CLAUDE_CONFIG_DIR) and tools= uses its tool names, which run-pi translates (Glob->find, and an unmappable name stops the step). The run-llm protocol (wfrun + file-based exchange) works on any agent platform with a subagent facility. A step that hits an underspecified fork can stop the run with a DECISION: request; answering needs nothing beyond wfrun (resume --answer), and decider=\"llm\" settles such forks unattended on both backends (schema-forced ruling on run-cc, text ruling on run-pi). On Windows both backends bypass the npm .cmd/.bat launcher, which corrupts multi-line/metachar prompts; for claude, if multiple installs are present the one earlier on PATH wins -- see the skill README's Requirements section."
---

# XML Workflow System v2 (xml-wf)

Decompose a task into single-responsibility steps in XML; the Python runner
`wfrun` executes them deterministically. Each step runs as an isolated
`claude -p` subagent (full context separation, file-based I/O), optionally
under a role (`role=` naming a .claude/agents definition, or an inline
`<role>` body) and an execution mode (`mode=`, bundled role-mode prompts).
The canonical spec is `references/spec.md`.

Always invoke the runner as (`${CLAUDE_SKILL_DIR}` resolves to this skill's directory):
```bash
WFRUN="env PYTHONPATH=${CLAUDE_SKILL_DIR}/scripts uv run python -m wfrun"
$WFRUN {validate|run|resume|plan|viz|prompt|record|poll|dispatch|wait|interp|eval|ask} ...
```

## Mode dispatch (decide from the arguments)

| Argument content | Mode | Procedure |
|---|---|---|
| `--build`, a task description, "create/build a workflow", 「ワークフロー化」, or a **partial/sketch `.xml` to complete/finish** | **Build** | Read and follow `references/build.md` |
| `--run-cc`, a **complete** `.xml` path, "run/execute" | **Run (batch)** | Read and follow `references/run-cc.md` |
| `--run-pi`, "run on pi", "run without claude" | **Run (batch, pi backend)** | Read and follow `references/run-pi.md` |
| `--resume`, a run dir path (contains `state.json`), "resume" | **Resume** | `references/run-cc.md` — "On failure / resume", or "On decision" when `state.json` says `awaiting-decision` |
| `--run-llm`, "run interactively", "supervise step by step" | **LLM orchestration** | Read and follow `references/run-llm.md` |

- For compound requests ("build it and run it"): Build → user approval → Run, in sequence
- The default execution mode is **batch (`wfrun run`, deterministic)**.
  Use --run-llm only when the user explicitly wants interactive, step-supervised execution
- **Batch has two backends and `wfrun run --backend` picks one.** The default
  `auto` reads `CLAUDE_CODE_SESSION_ID`: set → the claude CLI (run-cc), unset →
  the pi CLI (run-pi). `--run-cc` / `--run-pi` are the explicit forms. The
  backends are not interchangeable: run-pi refuses `schema=` and
  `on-error="debug"` at startup, and `references/run-pi.md` gives the rewrites.
  `resume` inherits the backend recorded in the run dir and never re-detects
- **Pass `--inherit-model <model>` with the model this session is currently
  running as** (a concrete identifier, not a canonical difficulty class like
  `basic`/`ultra` -- it is used as-is, not resolved through `model_map.json`).
  wfrun runs as an independent process with no way to detect the invoking
  session's own model, so a step with no `model=` of its own (no step
  attribute, no role-frontmatter default) needs this to avoid silently
  running on whatever model the backend CLI picks for itself. Omitting it is
  not an error -- a `note:` line at run start names the step(s) left that way
  -- but the choice is per-machine and undocumented, and not necessarily one
  the CLI calls a default (under pi, measured selecting an enabled provider
  over its own `defaultModel`), so pass it whenever this session's own model
  is known. Symmetric across both backends
- A bare `.xml` path is ambiguous between Run and Build. Resolve by intent, not
  by guessing: a complete workflow the user wants **executed** → Run; an
  incomplete sketch, or an explicit "complete/finish/fill in this XML" → Build.
  `$WFRUN validate <xml>` reveals whether it is structurally complete; when the
  intent is still unclear (e.g. an .xml arrives with change requests), ask

## Principles common to all modes

- **Control flow belongs to wfrun. Never interpret the XML yourself and perform
  steps on its behalf**
- Build mode ends at XML generation and `$WFRUN validate` — it never executes
  the task itself (no SQL, no data processing)
- Never report completion while validation errors remain
