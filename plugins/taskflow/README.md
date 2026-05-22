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

## Configuration

### `TASKFLOW_PROJECT_ROOTS`

Semicolon-separated list of `_projects/` root directories. Skills and scripts
use this variable to locate project data across multiple repositories.

```bash
export TASKFLOW_PROJECT_ROOTS="/path/to/repo-a/_projects;/path/to/repo-b/_projects"
```

When unset, taskflow falls back to `_projects/` in the current workspace.

To set it permanently in Claude Code, add it to your `settings.json`:

```json
{
  "env": {
    "TASKFLOW_PROJECT_ROOTS": "/path/to/repo-a/_projects;/path/to/repo-b/_projects"
  }
}
```

## Usage

### Specifying a project

Prefix the prompt with `pj:<project>`. If omitted, the LLM infers the project.

| Action | Example prompt |
|---|---|
| Specify a project | `pj:my-project fix the build error` |
| Specify a project + slash command | `pj:my-project /plan design the schema` |
| Discover projects | `pj:?` or `pj:? deploy pipeline` |
| No matching project | `pj:none write a README` |
| Create a new project | `create a new project called xxx` |
| Bypass taskflow entirely (this turn) | `norouter write a README` |

### /progress — task progress commands

`/progress` is the unified entry for inspecting and mutating task state. Natural-language input is parsed by the `progress-router` subagent into (action, targets), confirmed via `AskUserQuestion` (unless `-y`), and executed.

| Sub-action | Effect | Examples |
|---|---|---|
| `check` | Drift / stale / approval-pending detection. Read-only. | `/progress check` |
| `audit` | Classify every task by `## Next Steps` state (pending / completion candidate / untracked / clean). Read-only. | `/progress audit` |
| `rebuild` (alias `sync`) | Regenerate the progress.md table region from task files. | `/progress rebuild` |
| `approve` | Move a task `1_in_progress/ → 2_done/`. Human-approved transition. | `/progress approve 2026-05-14_xxx`<br>`/progress 完了 migration`<br>`/progress 全部完了 -y` |
| `revert` | Move backward one step (`1_in_progress → 0_todo`, or `2_done → 1_in_progress`). | `/progress revert <prefix>`<br>`/progress 戻して audit` |

**Action synonyms** (case-insensitive substring match on the input):

- approve: `approve`, `完了`, `終了`, `done`, `finish`, `ok`
- revert: `revert`, `戻す`, `戻し`, `undo`, `取り消し`
- `check` / `audit` / `sync` / `rebuild`: literal keywords only

**Target resolution** (highest-priority match wins):

1. Filename stem starts with the phrase (case-insensitive)
2. Substring of the filename stem
3. Semantic match against the H1
4. Plurality markers (`全部` / `all` / `両方` / `両`) match every candidate

**Flags**:

- `-y` / `--yes` — skip the confirmation prompt and execute immediately

For destructive actions (`approve` / `revert`), the main agent prints the resolved plan and asks via `AskUserQuestion` before any mutation. `-y` skips this when the target is already verified. When the router returns zero matches or ambiguous low-confidence candidates, it stops and lists candidates rather than guessing.

### /kanban — Kanban project board

`/kanban` generates a self-contained HTML kanban board showing all taskflow projects and their tasks. Tasks are organized by status (TODO / In Progress / Done) and by project, with priority badges, session history, and one-click navigation to session logs or `/progress` sub-actions.

The kanban board:
- Reads all projects from `_projects/index.md` and enumerates tasks
- Extracts session history from each task's `@log` block; resolves short session IDs to full UUIDs for clickable links
- Renders two views (switch via toggle): **By Status** (column-per-status) and **By Project** (column-per-project)
- Supports real-time project / status filtering via legend buttons
- Includes a `/progress` dropdown for quick access to `/progress check`, `/progress audit`, and `/progress rebuild`

Invocation:

| Method | Command | Result |
|---|---|---|
| Via skill | `/kanban` | Starts HTTP server at `http://localhost:17329/`, opens browser, blocks until Ctrl+C |
| Via script (static) | `uv run python scripts/generate_kanban.py` | Writes HTML to `/tmp/taskflow-kanban.html` |
| Via script (serve) | `uv run python scripts/generate_kanban.py --serve --open` | Starts server and opens browser |

Options for the script:

- `--out PATH` — Write HTML to a custom path (default: `/tmp/taskflow-kanban.html`)
- `--serve` — Start an HTTP server on `localhost:17329` with `/open?session=<UUID>` and `/open?prompt=<...>` endpoints for session / prompt launches
- `--open` — Open the result in the default browser after generation
- `--scheme vscode|vscodium` — Override the URI scheme (default: auto-detect)

### progress.md

`progress.md` is the task index. It has a free-text region (Architecture / Key Decisions / Open Issues / Reference Materials — human-edited) and an auto-generated table region (`<!-- @table:begin -->` ... `<!-- @table:end -->`) listing the TODO / In Progress / Completed tasks. Rebuild the table via `/progress rebuild`; never hand-edit inside the markers.

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
updated: 2026-05-14
---

# Task title (becomes the progress.md row summary)

Body (mutable region — replace freely).

## Next Steps
- remaining item 1
- remaining item 2

<!-- @log:begin -->
- 2026-05-13 [s:abc12345]: started
- 2026-05-14 [s:def67890]: phase A complete | next: write tests
<!-- @log:end -->
```

- The body region is mutable; the log block is **append-only**.
- `## Next Steps` non-empty = pending; empty in `1_in_progress/` = completion candidate. The `Stop` hook prompts the LLM to update this section based on actual session work (see [How it works](#how-it-works)).
- Log lines carry a `[s:<session-id-prefix>]` tag for downstream audit lookup.

Status transitions are performed via `/progress approve` and `/progress revert` (see above).

#### Migrating tasks from v0.2.0

If `/progress audit` reports any `UNTRACKED` tasks (files predating the `## Next Steps` requirement), retrofit them once with the repository-root migration script:

```bash
uv run python scripts/migrate_task_next_steps.py <project-root>
```

This is a one-shot tool; it lives outside the plugin distribution because it is repo maintenance, not runtime behavior.

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
  └─ [Stop hooks] ─→ archive plans/memory copies, AND
                     prompt the LLM to record next steps for touched tasks
```

### hooks

Three hooks run automatically when the plugin is enabled.

#### UserPromptSubmit: session_init.py

Runs every turn. Manages `_projects/_state/{session_id}.json` and injects `[Progress Session]` into the LLM context. Creates `_projects/`, `_projects/_state/`, and a template `_projects/index.md` if missing.

Also handles guidelines injection: on the first turn of a session (and after compaction), the full content of `progress_guidelines.md`, `notes_guidelines.md`, and `tasks_guidelines.md` is injected. On subsequent turns, only a keyword reminder (`guidelines_reminder.md`) is injected to maintain attention to the guidelines at lower token cost.

##### Maintaining guidelines_reminder.md

`prompts/guidelines_reminder.md` is a keyword reminder injected every turn after the first. It works by re-activating the LLM's attention to the full guidelines injected earlier in the conversation.

**Design principle**: the reminder contains distinctive terms from the source guidelines — particularly prohibitions, format-specific patterns, and authority definitions — that boost attention weight on the corresponding full-text passages.

**Maintenance rule**: when any of the 3 source guidelines (`progress_guidelines.md`, `notes_guidelines.md`, `tasks_guidelines.md`) is updated, `guidelines_reminder.md` MUST be updated in the same commit. Stale keywords that reference removed rules cause hallucinated constraints; missing keywords for new rules cause silent non-compliance.

**Keyword selection criteria** (in priority order):

1. Prohibitions (what NOT to do) — highest violation risk when forgotten
2. Format-specific patterns (frontmatter fields, filename conventions, character limits)
3. Authority definitions (which source of truth governs which field)

#### Stop: session_sync.py

Runs at session end. Copies plan/memory files modified within the last 10 minutes into the project directory.

#### Stop: session_progress_capture.py

Runs at session end alongside `session_sync.py`. Scans the session jsonl for write/edit/file-moving tool calls; if any are found, returns `{"decision":"block", "reason": ...}` with an English imperative asking the LLM to update each touched task's `## Next Steps` section (create a task in `0_todo/` or `1_in_progress/` if absent, clear it on completion). Fires at most once per session via a sidecar marker file (`{session_id}.captured`) to avoid conflicts with concurrent state rewrites. Touched files and the `[s:<session-id-prefix>]` tag are substituted at runtime. See `_projects/harness-taskflow/project-notes/specs/progress-audit-design.md` for the design.

## Known issues

- **State file race condition**: Multiple hooks (`session_init.py`, `session_compact_reset.py`) read and write the same `_projects/_state/{session_id}.json` without file locking. In practice the triggering events (`UserPromptSubmit` vs `SessionStart:compact`) do not fire concurrently, so data loss has not been observed. A future release may add atomic writes or advisory locking.
