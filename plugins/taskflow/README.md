# taskflow

A Claude Code plugin that manages progress and context across concurrent tasks. It binds sessions to projects and provides state transitions plus context injection through `progress.md`, `tasks/`, and `project-notes/`.

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

No manual setup is required. On the first user prompt in a workspace, taskflow's `UserPromptSubmit` hook creates `_projects/`, `_projects/_state/`, and a template `_projects/index.md` automatically.

> **Claude Code only.** taskflow's per-turn project routing depends on `UserPromptSubmit`'s `additionalContext` injection. Cursor's `beforeSubmitPrompt` (the third-party auto-mapped equivalent) cannot inject context into the LLM, so taskflow does not work on Cursor. See `_projects/harness-taskflow/project-notes/procedures/claude-plugin-to-cursor-compat.md` for background.

## Usage

### Specifying a project

Prefix the prompt with `pj:<project>`. If omitted, the LLM infers the project.

| Action | Example prompt |
|---|---|
| Specify a project | `pj:my-project fix the build error` |
| Specify a project + slash command | `pj:my-project /plan design the schema` |
| No matching project | `pj:none write a README` |
| Create a new project | `create a new project called xxx` |
| Bypass taskflow entirely (this turn) | `norouter write a README` |

### progress

`progress.md` is the task index. It has a free-text region (Architecture / Key Decisions / Open Issues / Reference Materials — human-edited) and an auto-generated table region (`<!-- @table:begin -->` ... `<!-- @table:end -->`) listing the TODO / In Progress / Completed tasks.

| Action | Example prompt |
|---|---|
| Review progress | `show the progress` |
| Rebuild the table | `/progress rebuild` |
| Detect drift | `/progress check` |

### tasks

One task per file under `tasks/<status>/<date>_<topic>.md`. Status is the folder.

```
tasks/
  0_todo/             not started
  1_in_progress/      started; work ongoing
  2_done/             complete (human-approved)
```

A task file structure:

```markdown
---
priority: HIGH
created: 2026-05-13
updated: 2026-05-13
---

# Task title (becomes the progress.md row summary)

Body (mutable region — replace freely).

<!-- @log:begin -->
- 2026-05-13: started
- 2026-05-14: phase A complete
<!-- @log:end -->
```

The body region is mutable; the log block is append-only.

Status transitions:

| Action | Example prompt |
|---|---|
| Start a task | `/progress sync` (after `mv` or after editing progress.md) |
| Approve completion | `/progress approve <id>` |
| Send back / reopen | `/progress revert <id>` |

### project-notes

Project-specific persistent knowledge, categorized by folder:

| Category | Purpose |
|---|---|
| `specs/` | Designs, decisions, ADRs |
| `investigations/` | Research, analysis, post-mortems |
| `checks/` | Verification items, checklists |
| `procedures/` | Step-by-step instructions for humans |
| `backlog/` | Candidate items, ideas |
| `_archive/` | Exhausted; no longer authoritative |

| Action | Example prompt |
|---|---|
| Save | `save this research result to notes` |
| List | `what's in notes?` |
| Record codebase structure | `summarize this repo's structure into notes` |

`project-notes/index.md` is a 4-column table (`File | Description | Tags | Updated`) tracking notes; updated automatically when notes are created or edited.

#### Auto-save for investigation-style tasks

When the user's intent is information gathering / comparison / structuring / investigation, the project-router detects it semantically and returns `project_notes_autosave: true`. The main agent delivers its primary answer, then asks the user whether to save — including a suggested category and slug. Only on approval are `project-notes/<category>/<slug>.md` and `project-notes/index.md` updated.

See `taskflow/prompts/project_router_agent.md` `Step 2b` for the detection conditions, and the "auto-save flow" section of `taskflow/prompts/notes_guidelines.md` for the save flow.

- Fires for: "investigate this repo's structure", "compare options A and B", "organize how X works"
- Does NOT fire for: "fix a typo in the README", one-shot explanation requests ("what is X?"), or explicit refusal ("don't save")

## Directory layout

```
_projects/
  index.md                    all-projects index
  _state/                     session state (auto-managed)
  <project>/
    index.md                  project overview
    progress.md               task index
    tasks/
      0_todo/                 not started
      1_in_progress/          started; work ongoing
      2_done/                 complete (human-approved)
    project-notes/
      index.md                4-column index
      specs/                  designs, decisions, ADRs
      investigations/         research, analysis, post-mortems
      checks/                 verification items, checklists
      procedures/             step-by-step instructions
      backlog/                candidate items, ideas
      _archive/               exhausted
    _archive/                 project-level archive
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
  │     needed     → read/write progress.md / tasks / project-notes
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

Runs every turn. Manages `_projects/_state/{session_id}.json` and injects `[Progress Session]` into the LLM context. Creates `_projects/`, `_projects/_state/`, and a template `_projects/index.md` if missing.

#### Stop: session_sync.py

Runs at session end. Copies plan/memory files modified within the last 10 minutes into the project directory.
