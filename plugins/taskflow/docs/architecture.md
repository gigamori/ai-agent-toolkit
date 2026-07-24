# taskflow internal architecture (v0.2.3)

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
| Task status (TODO / In Progress / Completed) | Folder of the task file | Move the file via `/progress start`/`approve`/`unstart` (the table is regenerated from task files by `/progress rebuild`; `sync` is an alias of `rebuild`) |
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
| `1_in_progress` → `0_todo` | Send back / postpone via `/progress unstart <id>` | Human invokes |
| `2_done` → `1_in_progress` | Reopen via `/progress start <id>` | Human invokes |

The router does NOT auto-promote tasks in v0.2.2. All transitions are user-driven via `/progress` sub-actions or explicit file moves.

### Approval gate

Transitioning into `2_done/` requires explicit human approval. The subagent emits a `stale_hint` if `1_in_progress/` items have not been updated for ≥ 14 days (suggests running `/progress check`). Note: `stale_hint` is an mtime-based approximation; the authoritative staleness check is `/progress check`, which reads the `updated:` frontmatter field.

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
| `start <id>` | Move a task `0_todo/ → 1_in_progress/` (also reopens `2_done/ → 1_in_progress/`). |
| `approve <id>...` | Move tasks `1_in_progress/ → 2_done/` (human-approved); clears the `## Next Steps` content on move. |
| `unstart <id>` | Move a task back to TODO (`1_in_progress → 0_todo`; from `2_done` it is a non-adjacent jump confirmed with a ⚠). |

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
  │  │   3. fork detection: if forked (a parent `[Progress Session]` marker — or
  │  │      a matching first user-entry — survives in a sibling JSONL; message
  │  │      uuids are rewritten on fork, so they do NOT identify the parent),
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
  │                    (reminder variant via env TASKFLOW_GUIDELINES_REMINDER: `full` default =
  │                    guidelines_reminder.md ~750 tok / `manifest` = guidelines_reminder_manifest.md
  │                    ~460 tok — guideline rules compressed to recall labels; the ROUTER /
  │                    RESPONSE-LEADING-LINES lines stay byte-identical full text in both;
  │                    falls back to full if the manifest file is missing)
  │     + project_index: index.md on project switch
  │     + project_rules: rules.md full body on switch / `##` heading manifest otherwise
  │                      (full every turn if frontmatter inject_every_turn: true; absent if no rules.md)
  │     + ACTION_REQUIRED banner: every turn while progress.md is missing
  │     + fork_context: first turn of a forked session
  │     (when current_project is empty and not discovery → NOTHING is injected; router not invoked)
  │
  ▼ [LLM] detects [Progress Session] with a non-empty current_project
  │  1. build a JSON context block (router spec lives in agents/project-router.md; do NOT inline it)
  │  2. invoke the project-router subagent via the Agent tool (subagent_type: taskflow:project-router)
  │
  ▼ [project-router subagent] runs on an isolated generation path (model: sonnet)
  │  1. determine the project (read-only; current_project is non-empty by gate)
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
  │
  ▼ [PostToolUse hooks] on each Write / Edit / NotebookEdit / Bash:
     • task_rebuild_progress.py — regenerate the progress.md table when a tasks/ file changed
     • touched_capture.py       — append the written path(s) to `{session_id}.touched`
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
  │     Note: the 10-minute window selects ALL recently-modified plans regardless
  │     of which project they belong to. The plans/ directory is an archive that
  │     may contain cross-project plans copied in the same window. This is
  │     intentional archive behavior, not a bug.
  │
  ▼ [Stop hook #2] session_progress_capture.py  (design: project-notes/specs/exec-binding.md)
     1. read `project` from state_file
        (self-heal: if empty, recover from a `[pj:...]` line in the assistant's last message)
     2. read the per-session `{session_id}.touched` ledger written by the PostToolUse hook
        `touched_capture.py` (NOT a jsonl scan / git diff); resolve touched task md by basename
     3. exec-binding: union-merge any `[tasks: a.md b.md]` carry from the assistant's last
        message into state `exec_bind`, then code-bind those owning tasks' `@log`
     4. gate (INV-1, no-loop): return {"decision":"block", ...} ONLY to (a) Round1-remind a
        missing touched task, (b) report a code auto-bind, or (c) report a new exec-bind skip
        — each bounded by the `{session_id}.bind` sidecar (reminded rounds + `exec_tried`).
        A task that can never be bound (no `@log:end`) does NOT loop the gate.
     bind writes are serialized by the bounded per-task advisory lock `log_lock.py` (INV-2)
```

## state_file

Path: `_projects/_state/{session_id}.json`

The hook (`session_init.py`) writes the full schema below. The project-router subagent is read-only and does not write state. Capture round-state is NOT a JSON field — it lives in sidecar files (to avoid clobbering by concurrent state rewrites): `{session_id}.bind` (Round1/Round2 reminder rounds + `exec_tried` skip records) and `{session_id}.touched` (the append-only touched-path ledger written by `touched_capture.py`). (`{session_id}.captured` is a legacy marker, no longer written — only swept by the 7-day cleanup.)

```json
{
  "project": "<current active project, or empty string>",
  "rules_loaded": true,
  "indexed_project": "<last project the index was injected for>",
  "guidelines_loaded": true,
  "project_rules_indexed": "<last project the rules.md full body was injected for>",
  "origin": "cc",
  "parent_session_id": "<parent session id if forked; absent otherwise>",
  "inherited_tasks": ["<task filenames inherited from the parent, if forked>"],
  "exec_bind": ["<owning task basenames carried via [tasks:]; union-merged, append-only>"]
}
```

`rules_loaded` / `guidelines_loaded` / `indexed_project` / `project_rules_indexed` gate the once-per-session (or per-switch) injections and are reset by `session_compact_reset.py` after auto-compaction. `project_rules_indexed` tracks the last project whose `rules.md` full body was injected; resetting it on compaction re-injects the primer (compaction summarizes the primer body away). `origin` identifies the generator (`cc` = Claude Code). `parent_session_id` / `inherited_tasks` appear only on forked sessions.

### Writers

| Actor | Timing | Condition |
|---|---|---|
| `session_init.py` (hook) | every turn once the workspace is opted-in (`_projects/` exists) — including projectless turns, which write empty-`project` state (swept after 7 days; see below) | writes the full schema; project from explicit `pj:` only — **no path inference** |
| `session_progress_capture.py` (hook) | session end | recovers `project` from a `[pj:...]` line (self-heal); union-merges `exec_bind` from a `[tasks:]` carry in the assistant's last message |
| `scripts/pj_rules.py reset-indexed` (via `/pj-rules` skill) | after a confirmed `rules.md` write | merge-preserving reset of `project_rules_indexed` only, so the updated body re-primes next turn; never hand-edited by the skill |

### Readers

| Actor | Timing | Purpose |
|---|---|---|
| `session_init.py` (hook) | 2nd turn onward | fetch `current_project` + injection flags |
| `session_sync.py` (hook) | session end | determine the copy-destination project |
| `session_progress_capture.py` (hook) | session end | determine the project for the capture prompt |
| `session_compact_reset.py` (hook) | on compaction | reset injection flags (other fields preserved) |

## `pj:` syntax

Place `pj:<project_name>` near the beginning of the prompt. It is recognized at the start of the prompt or after any whitespace, so it may follow other leading lines (e.g. `mode:`); it need NOT be the literal first line. **`pj:` (and `norouter`) are only recognized within the first 500 characters of the prompt** — occurrences beyond that window are ignored (prevents accidental project switches from literal mentions in body text).

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

When a session is forked, Claude Code copies the parent's JSONL transcript, rewriting `sessionId` **and** — in current Claude Code — each message `uuid`, so uuid comparison cannot identify the parent. On the first turn of a new session, `session_init.py` (`detect_parent_session`) instead relies on two signals that survive the copy: (1) **primary** — the parent's injected `[Progress Session] session_id=<parent>` marker, preserved verbatim: it scans the transcript head (`PARENT_MARKER_RE`, first `PARENT_SCAN_LINES` lines) for a `session_id` other than its own; (2) **fallback** — the first `type=user` entry's `(timestamp, message content)` pair, matched against the head of recent sibling JSONLs in the same directory. The child then inherits the parent's `project` from the parent state file, and scans the project's `1_in_progress/` tasks for the parent session's `[s:<sid>]` log tag (an `sid8` substring match) to populate `inherited_tasks`. A `[Forked Session]` block is injected on that first turn so the LLM continues the inherited tasks (logging under the new `session_id`).

### `origin` field

State files carry `origin: "cc"` (generator = Claude Code). `backfill_origin.py` is a one-shot utility that stamps `origin: "cc"` onto pre-existing state files that predate the field.

### Progress-capture self-heal

If `session_progress_capture.py` finds an empty `project` in state at session end, it recovers the project from a `[pj:<name>]` line in the assistant's last message (searched within the leading-line region, order-agnostic — the `[pj:]` line shares that region with other plugins' leading lines such as `[Mode:]`). This rescues cases where `session_init.py` failed to persist the project on the first turn, or a fork inherited an empty parent state.

### exec-binding (`[tasks:]` carry)

When a session does task work whose result lands OUTSIDE the task's own `tasks/<status>/*.md` file (execution-by-reference), the LLM lists the owning task filename(s) in a `[tasks: a.md b.md]` leading line. `session_progress_capture.py` union-merges these into the state `exec_bind` array and code-binds each owning task's `@log` (provenance note `(auto) executed via [tasks:] carry`), so the work is recorded even though `tasks/` was never edited. Bind failure (no `@log:end`) is surfaced once as `auto-skip(ambiguous)` and recorded in `exec_tried` to stop retrying (INV-1 c). The `[tasks:]` instruction is wired into the injected prompts (`project_routing.md` "Response leading lines" + `guidelines_reminder.md`), parallel to `[pj:]`; direct task-file edits need no `[tasks:]` (the PostToolUse `.touched` capture records them). See `project-notes/specs/exec-binding.md`.

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

### Hooks (CWD-fixed) vs. the command / viewer layer (`TASKFLOW_PROJECT_ROOTS`)

Project-root resolution is **deliberately split** between two layers, and the two are NOT unified:

| Layer | Resolves `_projects/` via | Rationale |
|---|---|---|
| **Hooks** (`session_init.py`, `session_sync.py`, `session_progress_capture.py`, `task_rebuild_progress.py`) | `os.getcwd()` — CWD-fixed, always the local `_projects/` | A hook fires inside one Claude Code session whose CWD *is* the workspace. Keeping it CWD-pure removes any cross-workspace ambiguity mid-session and needs no configuration. |
| **Command / viewer layer** (`/progress` skill, `scripts/generate_kanban.py`) | `$TASKFLOW_PROJECT_ROOTS` — a `;`-separated list of root dirs, first existing `<root>/<project>/` wins; falls back to `_projects/` in the CWD when unset | These are explicitly user-invoked (a viewer, an on-demand command) and may legitimately need to reach a project that lives under a different root than the current CWD. |

This boundary is intentional: the per-turn hook path stays CWD-local and side-effect-predictable, while the multi-root flexibility is confined to the on-demand command/viewer layer. (Unifying the two — e.g. making hooks honor `TASKFLOW_PROJECT_ROOTS` — was considered and rejected; a hook that silently retargets a different root mid-session would break the "one session ↔ one workspace" invariant the state files rely on.)

### When `_projects/` is absent

`session_init.py` (UserPromptSubmit) **bootstraps** `_projects/`, `_projects/_state/`, and a template `_projects/index.md` when they are missing — but only on the first prompt that includes an explicit `pj:<project>` or `pj:?` discovery. In a workspace that has **not** been opted in (no `_projects/`), a prompt without any `pj:` engagement `sys.exit(0)`s immediately without creating `_projects/`, so the plugin can be enabled in any workspace without side-effects until the user first engages taskflow. The other hooks (`session_sync.py`, `session_progress_capture.py`, and `task_rebuild_progress.py`'s project-dir check) treat a missing `_projects/` as a harmless no-op and `sys.exit(0)`.

Note: this no-side-effect property applies **only before opt-in**. Once `_projects/` exists, `session_init.py` writes a `_state/<session_id>.json` file **every turn**, including projectless turns (`pj:none`, `pj:?` discovery, a fork inheriting an empty parent, or simply no `pj:` while `_projects/` is present) — those produce an empty-`project` state file. This is deliberate (the F5a "stop writing projectless state" proposal was withdrawn: it would have broken the capture self-heal path and the fork-detection memo). Empty-`project` state is bounded by the 7-day stale-marker sweep in `session_progress_capture.py`'s `_cleanup_stale_markers` (non-empty `project` state is kept indefinitely, since `generate_kanban.py` resolves full UUIDs from it). The sweep's per-Stop delete budget (json + sidecar markers, combined) is capped at `TASKFLOW_SWEEP_MAX` (default 50, env-overridable); past-cutoff candidates are removed oldest-mtime-first, so a capped sweep still makes monotonic progress across Stops instead of re-selecting the same subset, and any deletion (or a cap hit) is logged to stderr. This follows a 2026-07-17 incident where an uncapped, unlogged sweep run against the real `_projects/_state/` with the wrong CWD deleted 250 session-state files in one Stop (see `project-notes/specs/capture-hook-sweep-sandbox.md`).

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
