---
name: xml-wf
description: "Build, run, and resume XML v2 workflows end to end. Use when the user wants to turn a task into an XML workflow (build, ワークフロー化), execute an existing workflow XML (--run-cc, 実行), resume a failed run (--resume, 再開), or have the LLM orchestrate step-by-step under supervision (--run-llm, 対話的に実行). The runner (wfrun) and the spec are bundled inside this skill."
argument-hint: "[--build|--run-cc|--run-llm|--resume] [task description|xml path|run dir]"
compatibility: "Requires Python 3.12+ and claude CLI v2.1.214+ (claude -p / --json-schema; role definitions are injected into prompts, --agent is not used). The skill definition and build/run-cc modes target Claude Code. The run-llm protocol (wfrun + file-based exchange) works on any agent platform with a subagent facility. On Windows, wfrun resolves the real claude executable itself (bypassing the npm .cmd/.bat launcher, which corrupts multi-line/metachar prompts); if multiple claude installs are present, the one earlier on PATH wins -- see the skill README's Requirements section."
---

# XML Workflow System v2 (xml-wf)

Decompose a task into single-responsibility steps in XML; the Python runner
`wfrun` executes them deterministically. Each step runs as an isolated
`claude -p` subagent (full context separation, file-based I/O) under an
explicit role (`role=` naming a .claude/agents definition, or an inline
`<role>` body) and optionally an execution mode (`mode=`, bundled role-mode
prompts). The canonical spec is `references/spec.md`.

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
| `--resume`, a run dir path (contains `state.json`), "resume" | **Resume** | `references/run-cc.md`, section "On failure / resume" |
| `--run-llm`, "run interactively", "supervise step by step" | **LLM orchestration** | Read and follow `references/run-llm.md` |

- For compound requests ("build it and run it"): Build → user approval → Run, in sequence
- The default execution mode is **--run-cc (wfrun batch, deterministic)**.
  Use --run-llm only when the user explicitly wants interactive, step-supervised execution
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
