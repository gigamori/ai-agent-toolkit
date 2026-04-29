# taskflow

A Claude Code plugin that manages progress and context across concurrent tasks. It binds sessions to projects and provides state transitions plus context injection through `progress.md`, `handoff/`, and `project-notes/`.

[日本語版 README はこちら](README_ja.md)

## Installation

### Via the plugin marketplace (recommended)

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install taskflow@ai-agent-toolkit
```

### Local (development / testing)

```bash
claude --plugin-dir ./plugins/taskflow
```

## Setup

Once the plugin is enabled, initialize it in the working directory:

```
/taskflow:init myproject
```

This creates the `_projects/` directory and writes hook settings into `.claude/settings.json`.

> **Claude Code only.** taskflow's per-turn project routing depends on `UserPromptSubmit`'s `additionalContext` injection. Cursor's `beforeSubmitPrompt` (the third-party auto-mapped equivalent) cannot inject context into the LLM, so taskflow does not work on Cursor. See `_projects/harness-taskflow/project-notes/claude-plugin-to-cursor-compat.md` for background.

## Usage

### Specifying a project

Prefix the prompt with `pj:<project>`. If omitted, the LLM infers the project.

| Action | Example prompt |
|---|---|
| Specify a project | `pj:my-project fix the build error` |
| Specify a project + slash command | `pj:my-project /plan design the schema` |
| No matching project | `pj:none write a README` |
| Create a new project | `create a new project called xxx` |
| Skip router invocation | `norouter write a README` |

### progress

Task state management: TODO / In Progress / Completed, plus a Session Log.

| Action | Example prompt |
|---|---|
| Review progress | `show the progress` |
| Record a session log | `write the session log` |

The `Prompt` column in the TODO table contains copy-pasteable prompts you can run directly.

### handoff

Detailed context attached to a task; used together with the instruction (`Prompt` column) in `progress.md`.

Three states:

| Folder | State | Description |
|---|---|---|
| `0_pending/` | Not started | Destination for new handoffs. Auto-consumed at session start. |
| `1_in_progress/` | In progress | Consumed by the AI. Selectively re-read in follow-up sessions. |
| `2_done/` | Done | Moved here only after human approval. |

Example actions:

| Action | Example prompt |
|---|---|
| Write a handoff | `write a handoff` |
| Approve completion | `mark this task as done` / `move handoff xxx.md to done` |
| Send back | `send this task back` |
| Hold | Append `[HOLD]` to the In Progress entry in `progress.md` |

Changing the status in `progress.md` moves the corresponding handoff folder accordingly.

### project-notes

Project-specific persistent knowledge. The AI references it selectively when needed.

| Action | Example prompt |
|---|---|
| Save | `save this research result to notes` |
| List | `what's in notes?` |
| Record codebase structure | `summarize this repo's structure into notes` |

`project-notes/index.md` tracks the file list and is updated automatically when notes are created or edited.

#### Auto-save for investigation-style tasks

When the user's intent is information gathering / comparison / structuring / investigation, the project-router detects it semantically and returns `project_notes_autosave: true`. The main agent delivers its primary answer, then asks the user whether to save — including a suggested filename. Only on approval are `project-notes/<slug>.md` and `project-notes/index.md` updated. A decline or no reply results in no save.

See `taskflow/prompts/project_router_agent.md` `Step 2b` for the detection conditions, and the "auto-save flow" section of `taskflow/prompts/notes_guidelines.md` for the save flow.

- Fires for: "investigate this repo's structure", "compare options A and B", "organize how handoff is used"
- Does NOT fire for: "fix a typo in the README", one-shot explanation requests ("what is X?"), or explicit refusal ("don't save")

## Directory layout

```
_projects/
  index.md                    all-projects index
  _state/                     session state (auto-managed)
  <project>/
    index.md                  project overview
    progress.md               task progress tracking
    project-notes/
      index.md                index for project-notes
      *.md                    individual project-notes
    handoff/
      0_pending/              not started
      1_in_progress/          in progress
      2_done/                 done (human-approved)
    plans/                    plan copies (auto-archived history)
    memory/                   memory copies (auto-archived history)
```

## How it works

### End-to-end flow

```
session start
  │
  ├─ [UserPromptSubmit hook] ─→ creates state_file + parses pj: + injects session info
  │
  ├─ [LLM] project determination (always) ─→ writes the project name to state_file
  │
  ├─ [LLM] applicability decision ─→ decides whether progress management is needed
  │     not needed → run the task only
  │     needed     → read/write progress.md / handoff / project-notes
  │
  ├─ [LLM] project_notes_autosave judgement ─→ for investigation intents, prompts to save after the main response
  │
  ├─ task execution
  │
  └─ [Stop hook] ─→ reads the project name from state_file and copies plans/memory
```

### hooks

Two hooks run automatically when the plugin is enabled.

#### UserPromptSubmit: session_init.py

Runs every turn. Manages `_projects/_state/{session_id}.json` and injects `[Progress Session]` into the LLM context. If `_projects/` is not present in CWD, it skips harmlessly.

#### Stop: session_sync.py

Runs at session end. Copies plan/memory files modified within the last 10 minutes into the project directory.
