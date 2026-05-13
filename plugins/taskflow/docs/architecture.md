# taskflow internal architecture (v2)

Internal design document for developers — read this when you need to understand or modify how the plugin works.

## Context-management types

### Four roles

| Type | Role | Lifetime | Audience | Context injection |
|---|---|---|---|---|
| `progress.md` | Task index: TODO / In Progress / Completed tables + free-text sections (Architecture / Key Decisions / Open Issues / Reference Materials) | Project lifetime | Human + AI | On apply, subagent reads the full file and returns it to the main agent |
| `tasks/` | One file per task; status by folder (`0_todo`/`1_in_progress`/`2_done`); body + append-only `<!-- @log -->` block | Task lifetime | AI + Human | Subagent lists `1_in_progress/`, selectively reads files relevant to the prompt, and returns them |
| `project-notes/` | Project-specific persistent knowledge, organized by category (`specs/`, `investigations/`, `checks/`, `procedures/`, `backlog/`, `_archive/`) | Project lifetime | AI | Subagent uses `index.md` (4-column) to select, reads only the relevant files, and returns them |
| `plans/`, `memory/` | Auto-archived copies of `~/.claude/` | Archive | Human | Never injected. Not to be referenced. |

### Role boundaries

- `progress.md` table region (between `<!-- @table:begin -->` and `<!-- @table:end -->`): auto-generated from task files. Never hand-edit.
- `progress.md` free-text sections: hand-edited; both LLM and human contribute.
- `tasks/<status>/<file>.md`: each file has frontmatter (priority, created, updated, optional dependencies), an H1 title (=summary shown in progress.md), a mutable body, and an append-only log block.
- `project-notes/<category>/`: reusable knowledge across tasks. Distill durable findings from `2_done/` tasks here.
- `auto-memory` (`~/.claude/projects/.../memory/`): a human-facing artifact; the LLM does not reference it directly.

## Single authority (v2 core principle)

| Field | Source of truth | How to change |
|---|---|---|
| Task status (TODO / In Progress / Completed) | Folder of the task file | Move the file, or run `/progress sync` after editing the table |
| Task summary | Task file H1 line | Edit in the task file body |
| Task priority | `priority:` in frontmatter | Edit frontmatter |
| Notes category | Folder under `project-notes/` | Move the file |
| Architecture / Key Decisions / Open Issues / Reference Materials | progress.md section content | Hand-edit |

The table region in `progress.md` is a **rebuilt cache**, not authoritative. The notes `index.md` is a metadata index, the body of each note is authoritative.

## tasks/ 3-state lifecycle

### Folder model

```
tasks/
  0_todo/             not started
  1_in_progress/      started; work ongoing
  2_done/             complete (human-approved)
```

### Transition rules

| Transition | Trigger | Actor |
|---|---|---|
| `0_todo` → `1_in_progress` | Decision to start; `mv` the file, or `/progress sync` after editing progress.md text | Human or LLM |
| `1_in_progress` → `2_done` | **Explicit human approval** via `/progress approve <id>` | Human invokes |
| `1_in_progress` → `0_todo` | Send back / postpone via `/progress revert <id>` | Human invokes |
| `2_done` → `1_in_progress` | Reopen via `/progress revert <id>` | Human invokes |

The router does NOT auto-promote tasks in v2. All transitions are user-driven via `/progress` sub-actions or explicit file moves.

### Approval gate

Transitioning into `2_done/` requires explicit human approval. The subagent emits a `stale_hint` if `1_in_progress/` items have not been updated for ≥ 14 days (suggests running `/progress check`).

### Coordination with `progress.md`

`progress.md` table rows are auto-generated from task files via `rebuild_progress.py`. To update a task's appearance in progress.md, edit the task file (frontmatter, H1, or folder) and run `/progress rebuild`. Direct table editing inside `<!-- @table -->` is forbidden.

## project-notes selective read

### Index-file approach

Each project has a `project-notes/index.md` — a four-column table: `File | Description | Tags | Updated`. The `File` column includes the category prefix (e.g., `specs/api-design.md`).

The subagent reads `index.md` once for the overview, then reads only the files whose Description / Tags match the prompt summary.

### Fixed taxonomy

| Category | Purpose |
|---|---|
| `specs/` | Designs, decisions, ADRs, proposals |
| `investigations/` | Research, analysis, post-mortems, retrospectives |
| `checks/` | Verification items, checklists (no judgment) |
| `procedures/` | Step-by-step instructions for humans |
| `backlog/` | Candidate items, ideas, issue tracker entries |
| `_archive/` | Exhausted; no longer authoritative |

Category is the **folder name**, not a frontmatter field. Move the file to change category.

### Fallback

If `index.md` is missing, walk `project-notes/**/*.md` recursively and match filenames against the prompt.

## `/progress` operations

Heavy operations live in slash commands (or a `pi.registerCommand`-equivalent on other agents), not the router:

| Sub-action | Effect |
|---|---|
| `/progress check` | Run drift / stale / approval-pending detection across 8 checks. Read-only. |
| `/progress sync` | Reconcile progress.md table status text ↔ folder location. |
| `/progress rebuild` | Regenerate the `<!-- @table -->` block from task files. |
| `/progress approve <id>...` | Move tasks from `1_in_progress/` to `2_done/`. |
| `/progress revert <id>` | Context-aware backward move (`1_in_progress → 0_todo`, or `2_done → 1_in_progress`). |

The router stays lightweight (per-turn cost). Heavy checks are explicitly invoked on demand.

## Session lifecycle

### Per-turn flow

```
user prompt
  │
  ▼ [UserPromptSubmit hook] session_init.py
  │  ├─ first turn: create state_file
  │  │   1. parse `pj:xx` from the first line of the prompt
  │  │   2. if absent, infer from the `_projects/<project>/` path
  │  │   3. write {"project": "..."} into state_file
  │  │
  │  ├─ subsequent turns:
  │  │   1. if `pj:xx` is present, update state_file
  │  │   2. otherwise read the current value from state_file
  │  │
  │  └─ every turn: inject additionalContext
  │     "[Progress Session] session_id=... state_file=... current_project=..."
  │     + when `pj:` is specified or the project is already set, additionally inject the body of project_routing.md
  │       (`taskflow/prompts/` paths are dynamically rewritten to the plugin's absolute path)
  │
  ▼ [LLM] detects [Progress Session]
  │  1. read project_router_agent.md
  │  2. prepend a JSON context block
  │  3. invoke the project-router subagent via the Agent tool
  │
  ▼ [project-router subagent] runs on an isolated generation path (haiku)
  │  1. write state_file (always)
  │  2. applicability decision (skip / apply)
  │  3. on apply: read index.md, progress.md, and the 3 guideline files
  │     (progress_guidelines / notes_guidelines / tasks_guidelines)
  │  4. tasks: list 1_in_progress/, selectively read relevant files;
  │     emit stale_hint if any are >14 days old
  │  5. project-notes: selective read via index.md (fallback: recursive walk)
  │  6. return a structured result (no auto-promotion in v2)
  │
  ▼ [LLM] receives the subagent result
  │  - apply → use as context for executing the task
  │  - skip  → skip project management
  │
  ▼ task execution
```

### Session end

```
session end
  │
  ▼ [Stop hook] session_sync.py
     1. read `project` from state_file
     2. empty project or directory missing → skip
     3. copy files modified in the last 10 minutes:
        ~/.claude/plans/*.md      → _projects/<project>/plans/
        ~/.claude/projects/       → _projects/<project>/memory/
          {encoded_cwd}/memory/*.md
```

## state_file

Path: `_projects/_state/{session_id}.json`

```json
{"project": "pi-studio-dev"}
```

### Writers

| Actor | Timing | Condition |
|---|---|---|
| hook (init) | every turn | `pj:` is specified, or path inference succeeded |
| subagent | when the project is finalized | the routing procedure has decided |
| main agent | when project can be inferred from conversation but is empty | as documented in project_routing.md |

### Readers

| Actor | Timing | Purpose |
|---|---|---|
| hook (init) | 2nd turn onward | fetch the `current_project` value for injection |
| hook (sync) | session end | determine the copy destination project |

## `pj:` syntax

Place `pj:<project_name>` on the first line of the prompt.

| Input | Effect |
|---|---|
| `pj:pi-studio-dev` | Set the project |
| `pj:none` | Declare no matching project |
| omitted | Keep the existing value; the LLM infers from context |

### Rationale

The YAML `key: value` form is a shape the model already recognizes as a metadata declaration. `pj=xx` (clashes with shell variables), `#pj=xx` (clashes with H1), `@pj=xx` (clashes with `@mention`), and `project=xx` (too long) were all considered and rejected in favor of `pj:xx`.

## subagent delegation — design decision

### Problem

When routing and task execution share a single generation path, they compete for attention. On technically dense tasks, routing was repeatedly skipped.

### Resolution

Delegate routing to a dedicated subagent (haiku, isolated generation path). The main agent's system prompt keeps only a short instruction to "invoke the subagent." Attention is now separated.

The router subagent is intentionally lightweight: per-turn reads + decisions only. Heavy operations (drift detection, rebuild, approval bulk moves) live in the `/progress` slash command which runs on user demand.

## Path resolution

### Paths used by hooks

| Path | Resolution |
|---|---|
| `_projects/` | `os.getcwd() + '/_projects'` (CWD-based) |
| `prompts/` | derived from `__file__` back to the plugin root |
| `~/.claude/plans/` | `os.path.expanduser` |
| `~/.claude/projects/.../memory/` | encode CWD (`lower().replace(':', '-').replace('/', '-')`) |

### When `_projects/` is absent

If `_projects/` is not in CWD, the hook immediately `sys.exit(0)`s as a harmless no-op. The plugin can be enabled without affecting projects that have not been initialized.

## Directory layout

```
_projects/
  index.md               all-projects index
  _state/                session state (hook-managed)
    {session_id}.json
  <project>/
    index.md             project overview
    progress.md          task index (free-text sections + auto-regenerated table region)
    tasks/
      0_todo/            not started
      1_in_progress/     started; work ongoing
      2_done/            complete (human-approved)
    project-notes/
      index.md           4-column: File | Description | Tags | Updated
      specs/             designs, decisions, ADRs
      investigations/    research, analysis, post-mortems
      checks/            verification items, checklists
      procedures/        step-by-step instructions
      backlog/           candidate items, ideas
      _archive/          exhausted
    _archive/            project-level archive (legacy pre-v2 files, etc.)
    plans/               plan copies (archived by Stop hook)
    memory/              memory copies (archived by Stop hook)
```
