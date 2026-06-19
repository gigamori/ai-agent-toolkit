# taskflow internal architecture (v0.2.2)

Internal design document for developers — read this when you need to understand or modify how the plugin works.

## Context-management types

### Four roles

| Type | Role | Lifetime | Audience | Context injection |
|---|---|---|---|---|
| `progress.md` | Task index: TODO / In Progress / Completed tables + free-text sections (Architecture / Key Decisions / Open Issues / Reference Materials) | Project lifetime | Human + AI | On apply, subagent reads the full file and returns it to the main agent |
| `tasks/` | One file per task; status by folder (`0_todo`/`1_in_progress`/`2_done`); body + append-only `<!-- @log -->` block | Task lifetime | AI + Human | Subagent lists `1_in_progress/`, selectively reads files relevant to the prompt, and returns them |
| `project-notes/` | Project-specific persistent knowledge, organized by category (`specs/`, `investigations/`, `checks/`, `procedures/`, `backlog/`, `_archive/`) | Project lifetime | AI | Subagent returns **pointers only** — the file list plus verbatim matching rows of `index.md`; it does NOT read or return note bodies (the main agent reads note files itself when needed) |
| `plans/`, `memory/` | Auto-archived copies of `~/.claude/` | Archive | Human | Never injected. Not to be referenced. |

### Role boundaries

- `progress.md` table region (between `<!-- @table:begin -->` and `<!-- @table:end -->`): auto-generated from task files. Never hand-edit.
- `progress.md` free-text sections: hand-edited; both LLM and human contribute.
- `tasks/<status>/<file>.md`: each file has frontmatter (priority, created, updated, optional dependencies), an H1 title (=summary shown in progress.md), a mutable body, and an append-only log block.
- `project-notes/<category>/`: reusable knowledge across tasks. Distill durable findings from `2_done/` tasks here.
- `auto-memory` (`~/.claude/projects/.../memory/`): a human-facing artifact; the LLM does not reference it directly.

## Single authority (v0.2.2 core principle)

| Field | Source of truth | How to change |
|---|---|---|
| Task status (TODO / In Progress / Completed) | Folder of the task file | Move the file via `/progress start`/`approve`/`revert` (the table is regenerated from task files by `/progress rebuild`; `sync` is an alias of `rebuild`) |
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
| `0_todo` → `1_in_progress` | `/progress start <id>`, or `mv` the file directly | Human or LLM |
| `1_in_progress` → `2_done` | **Explicit human approval** via `/progress approve <id>` | Human invokes |
| `1_in_progress` → `0_todo` | Send back / postpone via `/progress revert <id>` | Human invokes |
| `2_done` → `1_in_progress` | Reopen via `/progress revert <id>` | Human invokes |

The router does NOT auto-promote tasks in v0.2.2. All transitions are user-driven via `/progress` sub-actions or explicit file moves.

### Approval gate

Transitioning into `2_done/` requires explicit human approval. The subagent emits a `stale_hint` if `1_in_progress/` items have not been updated for ≥ 14 days (suggests running `/progress check`).

### Coordination with `progress.md`

`progress.md` table rows are auto-generated from task files via `rebuild_progress.py`. To update a task's appearance in progress.md, edit the task file (frontmatter, H1, or folder) and run `/progress rebuild`. Direct table editing inside `<!-- @table -->` is forbidden.

## project-notes (pointer surfacing)

### Index-file approach

Each project has a `project-notes/index.md` — a four-column table: `File | Description | Tags | Updated`. The `File` column includes the category prefix (e.g., `specs/api-design.md`).

The subagent surfaces project-notes as **pointers only**: it reads `index.md` and returns (a) the file list under `project-notes/` and (b) the verbatim rows of `index.md` whose Description / Tags match the prompt summary. It does NOT read note body files, and does NOT summarize, translate, or merge. The main agent reads the note files themselves when it needs their contents.

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

### No fallback walk

The router does NOT walk `project-notes/**/*.md` as a fallback. If a note exists on disk, it has a row in `index.md`; an unregistered note is drift, detected by `/progress check`, not the router. (The walk-the-tree fallback documented in `notes_guidelines.md` applies to the main agent's own note loading, not to the router.)

## `/progress` operations

`/progress` is a slash command (skill: `skills/progress/SKILL.md`). Natural-language input is parsed by the **progress-router subagent** (`agents/progress-router.md`, `model: sonnet`, read-only) into `(action, targets)`; the main agent confirms via `AskUserQuestion` (skipped with `-y`) and executes. This is a separate subagent from the per-turn project-router.

| Sub-action | Effect |
|---|---|
| `check` | Run drift / stale / approval-pending detection across 8 checks (`check_progress.py`). Read-only. |
| `audit` | Classify each task by `## Next Steps` state: pending / completion_candidate / untracked / clean (`audit_progress.py`). Read-only. |
| `rebuild` (alias `sync`) | Regenerate the `<!-- @table -->` block from task files (`rebuild_progress.py`). |
| `start <id>` | Move a task `0_todo/ → 1_in_progress/`. |
| `approve <id>...` | Move tasks `1_in_progress/ → 2_done/` (human-approved); clears the `## Next Steps` content on move. |
| `revert <id>` | Context-aware backward move (`1_in_progress → 0_todo`, or `2_done → 1_in_progress`). |

The per-turn project-router stays lightweight. Heavy checks and state transitions are explicitly invoked on demand via `/progress`.

## Session lifecycle

### Per-turn flow

```
user prompt
  │
  ▼ [UserPromptSubmit hook] session_init.py
  │  ├─ first turn: create state_file
  │  │   1. parse the first `pj:xx` (at start or after any whitespace; not necessarily line 1)
  │  │   2. if absent: keep empty — NO path-based inference
  │  │   3. fork detection: if forked (shared message uuid in another JSONL),
  │  │      inherit `project` + `inherited_tasks` from the parent state
  │  │   4. write the state_file (see "state_file" schema below)
  │  │
  │  ├─ subsequent turns:
  │  │   1. if `pj:xx` is present, update the project
  │  │   2. otherwise keep the current value from state_file (no inference)
  │  │
  │  └─ injection (only when current_project is non-empty, or `pj:?` discovery):
  │     "[Progress Session] session_id=... state_file=... current_project=..."
  │     + static_rules: body of project_routing.md (once per session)
  │     + guidelines:  full 3 files on first turn / after compact; keyword reminder otherwise
  │     + project_index: index.md on project switch
  │     + ACTION_REQUIRED banner: every turn while progress.md is missing
  │     + fork_context: first turn of a forked session
  │     (when current_project is empty and not discovery → NOTHING is injected; router not invoked)
  │
  ▼ [LLM] detects [Progress Session] with a non-empty current_project
  │  1. build a JSON context block (router spec lives in agents/project-router.md; do NOT inline it)
  │  2. invoke the project-router subagent via the Agent tool (subagent_type: project-router)
  │
  ▼ [project-router subagent] runs on an isolated generation path (model: sonnet)
  │  1. determine the project and write state_file ({"project": "..."}) — always
  │  2. applicability decision (skip / apply)
  │  2b. project_notes_autosave decision (true for investigation / analysis intents)
  │  3. on apply: read index.md and progress.md
  │     (the 3 guideline files are injected by the hook, NOT read by the subagent)
  │  4. tasks: list 1_in_progress/, selectively read relevant files;
  │     emit stale_hint if any are >14 days old
  │  5. project-notes: pointer-only via index.md (no fallback walk)
  │  6. return a structured result (no auto-promotion in v0.2.2)
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
  ▼ [Stop hook #1] session_sync.py
  │  1. read `project` from state_file
  │  2. empty project or directory missing → skip
  │  3. copy files modified in the last 10 minutes:
  │     ~/.claude/plans/*.md                          → _projects/<project>/plans/
  │     ~/.claude/projects/{encoded_cwd}/memory/*.md  → _projects/<project>/memory/
  │
  ▼ [Stop hook #2] session_progress_capture.py
     1. read `project` from state_file
        (self-heal: if empty, recover from a `[pj:...]` line in the assistant's last message)
     2. scan the session JSONL for Write / Edit / NotebookEdit / file-moving Bash (mv/cp/rm)
     3. if any files were touched → return {"decision":"block", "reason": ...} asking the
        LLM to update the touched tasks' `## Next Steps`; fires once per session
        (sidecar marker {session_id}.captured)
```

## state_file

Path: `_projects/_state/{session_id}.json`

The hook (`session_init.py`) writes the full schema below. The project-router subagent writes only `{"project": "..."}`. The "already captured this session" flag is NOT a JSON field — it is a sidecar marker file (`{session_id}.captured`), to avoid clobbering by concurrent state rewrites.

```json
{
  "project": "<current active project, or empty string>",
  "rules_loaded": true,
  "indexed_project": "<last project the index was injected for>",
  "guidelines_loaded": true,
  "origin": "cc",
  "parent_session_id": "<parent session id if forked; absent otherwise>",
  "inherited_tasks": ["<task filenames inherited from the parent, if forked>"]
}
```

`rules_loaded` / `guidelines_loaded` / `indexed_project` gate the once-per-session injections and are reset by `session_compact_reset.py` after auto-compaction. `origin` identifies the generator (`cc` = Claude Code). `parent_session_id` / `inherited_tasks` appear only on forked sessions.

### Writers

| Actor | Timing | Condition |
|---|---|---|
| `session_init.py` (hook) | every turn (while project active or `pj:?`) | writes the full schema; project from explicit `pj:` only — **no path inference** |
| project-router subagent | each turn it is invoked | writes `{"project": "..."}` |
| `session_progress_capture.py` (hook) | session end | self-heal only: recovers `project` from a `[pj:...]` line in the assistant's last message when state is empty |

### Readers

| Actor | Timing | Purpose |
|---|---|---|
| `session_init.py` (hook) | 2nd turn onward | fetch `current_project` + injection flags |
| `session_sync.py` (hook) | session end | determine the copy-destination project |
| `session_progress_capture.py` (hook) | session end | determine the project for the capture prompt |
| `session_compact_reset.py` (hook) | on compaction | reset injection flags (other fields preserved) |

## `pj:` syntax

Place `pj:<project_name>` near the beginning of the prompt. It is recognized at the start of the prompt or after any whitespace, so it may follow other leading lines (e.g. `mode:`); it need NOT be the literal first line.

| Input | Effect |
|---|---|
| `pj:pi-studio-dev` | Set / switch the project |
| `pj:none` | Clear the project (declare no matching project) |
| `pj:?` | Discovery: rank `_projects/index.md` by relevance; does NOT change the active project |
| `norouter` | Bypass taskflow entirely for this turn (the hook exits without writing state or injecting) |
| omitted | Keep the existing value; the project is NOT inferred from context |

### Rationale

The YAML `key: value` form is a shape the model already recognizes as a metadata declaration. `pj=xx` (clashes with shell variables), `#pj=xx` (clashes with H1), `@pj=xx` (clashes with `@mention`), and `project=xx` (too long) were all considered and rejected in favor of `pj:xx`.

## subagent delegation — design decision

### Problem

When routing and task execution share a single generation path, they compete for attention. On technically dense tasks, routing was repeatedly skipped.

### Resolution

Delegate routing to a dedicated subagent (`model: sonnet`, isolated generation path). The main agent's system prompt keeps only a short instruction to "invoke the subagent." Attention is now separated.

The router subagent is intentionally lightweight: per-turn reads + decisions only. Heavy operations (drift detection, rebuild, approval bulk moves) live in the `/progress` slash command which runs on user demand.

## Additional mechanisms

### Fork inheritance

When a session is forked, Claude Code copies the parent's JSONL transcript (rewriting `sessionId`) but preserves each message `uuid`. On the first turn of a new session, `session_init.py` reads the first transcript entry's `uuid` and looks for it in other recent JSONL files in the same directory; a match identifies the parent. The child then inherits the parent's `project` from the parent state file, and scans the project's `1_in_progress/` tasks for the parent session's `[s:<sid>]` log tag to populate `inherited_tasks`. A `[Forked Session]` block is injected on that first turn so the LLM continues the inherited tasks (logging under the new `session_id`).

### `origin` field

State files carry `origin: "cc"` (generator = Claude Code). `backfill_origin.py` is a one-shot utility that stamps `origin: "cc"` onto pre-existing state files that predate the field.

### Progress-capture self-heal

If `session_progress_capture.py` finds an empty `project` in state at session end, it recovers the project from a `[pj:<name>]` line in the assistant's last message (searched within the leading-line region, order-agnostic — the `[pj:]` line shares that region with other plugins' leading lines such as `[Mode:]`). This rescues cases where `session_init.py` failed to persist the project on the first turn, or a fork inherited an empty parent state.

### ACTION_REQUIRED preflight banner

While a project is active but its `progress.md` does not yet exist, `session_init.py` injects an `!!ACTION_REQUIRED (preflight)` banner every turn (not gated by session flags), instructing the LLM to ask for approval and then scaffold `index.md`, `progress.md`, and `project-notes/index.md` (and add the matching row to `_projects/index.md`) before starting user work. This scaffolding is permitted even inside Plan mode.

## Path resolution

### Paths used by hooks

| Path | Resolution |
|---|---|
| `_projects/` | `os.getcwd() + '/_projects'` (CWD-based) |
| `prompts/` | derived from `__file__` back to the plugin root |
| `~/.claude/plans/` | `os.path.expanduser` |
| `~/.claude/projects/.../memory/` | encode CWD (`lower().replace(':', '-').replace('/', '-')`) |

### When `_projects/` is absent

`session_init.py` (UserPromptSubmit) **bootstraps** `_projects/`, `_projects/_state/`, and a template `_projects/index.md` when they are missing — so the directory is created on the first prompt. The other hooks (`session_sync.py`, `session_progress_capture.py`, and `task_rebuild_progress.py`'s project-dir check) treat a missing `_projects/` as a harmless no-op and `sys.exit(0)`. The plugin can thus be enabled without affecting projects that have not been initialized.

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
    _archive/            project-level archive (legacy pre-v0.2.2 files, etc.)
    plans/               plan copies (archived by Stop hook)
    memory/              memory copies (archived by Stop hook)
```
