# taskflow

A Claude Code plugin that manages progress and context across concurrent tasks. It binds sessions to projects and provides state transitions plus context injection through `progress.md`, `tasks/`, and `project-notes/`.

[日本語版 README はこちら](README_ja.md)

> **New to taskflow?** Start with [Get Started](GET_STARTED.md) for the first-success walkthrough. The [user guide](USER_GUIDE.md) is the detailed usage reference; this README is the feature reference; [`docs/architecture.md`](docs/architecture.md) is the internal design.

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

No manual setup is required. `_projects/`, `_projects/_state/`, and a template `_projects/index.md` are created automatically on the first use of `pj:<project>` in a workspace.

> **Claude Code only.** taskflow's per-turn project routing depends on `UserPromptSubmit`'s `additionalContext` injection. Cursor's `beforeSubmitPrompt` (the third-party auto-mapped equivalent) cannot inject context into the LLM, so taskflow does not work on Cursor. See `_projects/harness-taskflow/project-notes/procedures/claude-plugin-to-cursor-compat.md` (development-repo design notes; not shipped with the plugin) for background.

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

### `TASKFLOW_GUIDELINES_REMINDER`

Selects which per-turn guidelines reminder variant `session_init.py` injects: `full` (default, ~750 tok — the complete keyword reminder) or `manifest` (~460 tok — the same prohibitions/format/authority/notes/autosave/task-write rules compressed to recall labels; the per-turn ROUTER and RESPONSE LEADING LINES lines stay full text in both variants). Unknown or unset values fall back to `full`.

```json
{
  "env": {
    "TASKFLOW_GUIDELINES_REMINDER": "manifest"
  }
}
```

`manifest` trades lower per-turn cost for weaker inline visibility of the conditional rules (PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE) — it relies on the full guidelines injected at session start (and after compaction) rather than repeating them every turn.

### `TASKFLOW_DONE_ROWS_MAX`

Caps the `progress.md` Completed table to the most recent N rows when `/progress rebuild` (or the auto-rebuild hook) regenerates it — the Completed section otherwise grows without bound as `tasks/2_done/` accumulates. Default: `10`. Set to `0` or a negative number for unlimited. When the cap is active, a footnote line reports how many older rows were omitted; the full history always remains in `tasks/2_done/`.

```json
{
  "env": {
    "TASKFLOW_DONE_ROWS_MAX": "20"
  }
}
```

`scripts/rebuild_progress.py --done-rows-max N` overrides the environment variable for a single invocation.

## Usage

### Specifying a project

Put `pj:<project>` near the beginning of the prompt — it is recognized at the start or after any whitespace, so it may follow other leading lines (e.g. `mode:`); it need not be the literal first line. **`pj:` must appear within the first 500 characters of the message to be recognized.** If omitted, the previously set project (if any) is kept; the project is NOT inferred from context.

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
| `start` | Move a task `0_todo/ → 1_in_progress/` (also reopens a done task: `2_done/ → 1_in_progress/`). | `/progress start 2026-05-14_xxx`<br>`/progress 着手 migration` |
| `approve` | Move a task `1_in_progress/ → 2_done/`. Human-approved transition. (From `0_todo/` it is a non-adjacent jump, confirmed with a ⚠.) | `/progress approve 2026-05-14_xxx`<br>`/progress 完了 migration`<br>`/progress 全部完了 -y` |
| `unstart` | Move a task back to TODO (`1_in_progress → 0_todo`; from `2_done` it is a non-adjacent jump, confirmed with a ⚠). | `/progress unstart <prefix>`<br>`/progress migration を未着手に` |

**State-goal synonyms** — the user names the state a task should reach (English tokens match on word boundaries — no partial-word hits, and never inside paths; Japanese tokens match as substrings with the longest overlapping token winning; case-insensitive):

- approve (`2_done`): `完了`, `終了`, `done`, `finish`, `approve`
- start (`1_in_progress`): `着手`, `開始`, `再開`, `進行中`, `start`, `begin`, `resume`
- unstart (`0_todo`): `未着手`, `着手前`, `開始前`, `todo`, `unstart`
- `check` / `audit` / `sync` / `rebuild`: literal keywords only
- undo/revert vocabulary (`戻す` / `undo` / `revert` / `取り消し`) is deliberately NOT claimed — those words mean "undo an LLM action" (e.g. a global revert skill owns them); such input resolves to `unknown` and moves nothing.

**Target resolution** (highest-priority match wins):

1. Filename stem starts with the phrase (case-insensitive)
2. Substring of the filename stem
3. Semantic match against the H1
4. Plurality markers (`全部` / `all` / `両方` / `両`) match every candidate

**Flags**:

- `-y` / `--yes` — skip the confirmation prompt and execute immediately

For destructive actions (`approve` / `start` / `unstart`), the main agent prints the resolved plan and asks via `AskUserQuestion` before any mutation. `-y` skips this when the target is already verified. When the router returns zero matches or ambiguous low-confidence candidates, it stops and lists candidates rather than guessing.

### /pj-rules — per-project rules

`/pj-rules` views or edits a project's optional `_projects/<project>/rules.md` — normative rules scoped to the taskflow project (`pj:`), not to a filesystem path. It runs no router subagent (the action set is small and a write's body is authored by the main agent regardless); intent is classified inline against a small synonym table.

| Action | What it does | Example |
|---|---|---|
| `show` (alias `list`) | Print the rules, their `## ` heading count, and line count vs. the cap. Read-only, no confirmation. | `/pj-rules show` |
| `write` | Add or edit a rule, or change `inject_every_turn`/`max_lines` frontmatter. Always shown as a diff and confirmed via `AskUserQuestion` before applying. | `/pj-rules add a rule: never edit dist/ directly` |

**No `-y` skip for `write`.** Unlike `/progress`, this skill has no confirmation bypass: `rules.md` is injected into every future turn of the project, so its blast radius extends well past the current turn. `-y`/`--yes` in the input is silently ignored.

After a confirmed write, `scripts/pj_rules.py show` is run before and after to verify the edit produced a `## ` heading (not just trust the model's self-report), then `scripts/pj_rules.py reset-indexed` resets only the session state's `project_rules_indexed` field (merge-preserving — other state fields are untouched) so the updated full text is shown again on the next turn.

### /kanban — Kanban project board

`/kanban` generates a self-contained HTML kanban board showing all taskflow projects and their tasks. Tasks are organized by status (TODO / In Progress / Done) and by project, with priority badges, session history, and one-click navigation to session logs or `/progress` sub-actions.

The kanban board:
- Reads all projects from `_projects/index.md` and enumerates tasks
- Extracts session history from each task's `@log` block; resolves short session IDs to full UUIDs for clickable links
- Renders two views (switch via toggle): **By Status** (column-per-status) and **By Project** (column-per-project)
- Supports real-time project / status filtering via legend buttons, plus a manual light / dark theme toggle
- Includes a `/progress` dropdown for quick access to `/progress check`, `/progress audit`, and `/progress rebuild`
- Launches a task into Claude Code via a **▶ CC** button on each card (pre-fills `pj:<project> @<task-file>`)
- Opens each task's Markdown in an in-browser viewer (**📄**, serve mode): sanitized rendering with clickable file references (in-modal navigation with a back button) and inline images
- Surfaces unreferenced sessions: a **No Task** column / per-project section for CC sessions attributed to a project but linked to no task, and a rightmost **No Project** column for CC sessions with no project at all (newest first, capped)

Invocation:

| Method | Command | Result |
|---|---|---|
| Via skill | `/kanban` | Starts the server in the background at a workspace-derived `http://localhost:<port>/` (idempotent — reports `already serving` if one is running for this workspace); prints the URL and the `--stop` command (does not block) |
| Via script (static) | `uv run scripts/generate_kanban.py` | Writes HTML to `/tmp/taskflow-kanban.html` |
| Via script (serve) | `uv run scripts/generate_kanban.py --serve --open` | Starts server and opens browser |

Each workspace's server binds a port derived from its `_projects` roots (base `17329`, span 64 — deterministic across processes via `hashlib`, so a later `--stop` from the same workspace finds the same server). Running `/kanban` from multiple VSCode workspaces at once no longer collides: each gets its own port, and `/health` carries a workspace-identity key so a same-port hash collision between two different workspaces is never mistaken for "already serving".

> **Claude Code open links** (the **▶ CC** button and session / prompt launches) require the Claude Code VS Code / VSCodium extension (`anthropic.claude-code`). In serve mode the launcher CLI and the extension are probed once per server; if neither `code` nor `codium` is on `PATH`, or the extension is not installed, the link returns a short page carrying the session UUID / prompt so you can open it manually — instead of failing silently or crashing the request.

> **`CLAUDE_CONFIG_DIR`** — if you relocate Claude Code's config directory, use **one value machine-wide**, as an **absolute path**, identical across the Claude Code session, the `kanban serve` process, and the VS Code extension host. Per-workspace values are **not supported**: the extension host's environment is outside taskflow's control, and taskflow's data model records only session UUIDs, not which config dir they came from — so a divergent setup fails silently (▶ CC links and session links stop resolving). The kanban reader scans both `$CLAUDE_CONFIG_DIR` and `~/.claude` and prefers the env one on a UUID collision; the session-sync hook uses the env value alone. Note that Claude Code reads the value **literally** — `~` is not expanded and relative values resolve against the current working directory, so `~/foo` creates a config dir named `~` under your CWD.

Options for the script:

- `--out PATH` — Write HTML to a custom path (default: system temp directory / `taskflow-kanban.html`)
- `--serve` — Start an HTTP server on a workspace-derived port; endpoints: `/open?session=<UUID>` and `/open?prompt=<...>` (session / prompt launches), `/md?path=<file>` (sanitized Markdown render), `/file?path=<file>` (project-scoped image / attachment serving), `/health`
- `--stop` — Stop this workspace's running `--serve` instance (by its `/health` pid)
- `--stop --all` — Stop every kanban server across all workspaces
- `--port PORT` — Use an explicit port instead of the derived one (applies to `--serve` and `--stop`)
- `--open` — Open the result in the default browser after generation
- `--scheme vscode|vscodium` — Override the URI scheme (default: auto-detect)

### progress.md

`progress.md` is the task index. It has a free-text region (Architecture / Key Decisions / Open Issues / Reference Materials — human-edited) and an auto-generated table region (`<!-- @table:begin -->` ... `<!-- @table:end -->`) listing the TODO / In Progress / Completed tasks. Rebuild the table via `/progress rebuild`; never hand-edit inside the markers. The Completed section is capped to the most recent `TASKFLOW_DONE_ROWS_MAX` rows (default 10) with a footnote showing the omitted count when capped — see [Configuration](#taskflow_done_rows_max); the full history always remains in `tasks/2_done/`.

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
- `## Next Steps` non-empty = pending; empty in `1_in_progress/` = completion candidate. The guidelines instruct the agent to maintain `## Next Steps` at the end of each turn that advances a task; `/progress audit` verifies it in code (see [How it works](#how-it-works)).
- Log lines carry a `[s:<session-id-prefix>]` tag for downstream audit lookup.
- A task may also carry an auto-managed `<!-- @notes:begin/end -->` block (placed right after `@log:end`) that lists related `project-notes/` paths. It is written by the note↔task link mechanism (see [How it works](#how-it-works)); never hand-edit it.

Status transitions are performed via `/progress start`, `/progress approve`, and `/progress unstart` (see above).

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

`project-notes/index.md` is a 4-column table (`File | Description | Tags | Updated`) tracking notes; the LLM keeps it in sync (prompted by the PreToolUse hook) when notes are created or edited.

The project-router surfaces project-notes as **pointers only** — the file list plus the verbatim matching rows of `project-notes/index.md`, never note body contents — so it cannot summarize, translate, or confabulate note contents into the routing result. The main agent reads the note files themselves when needed.

#### Auto-save for investigation-style tasks

When the user's intent is information gathering / comparison / structuring / investigation, the project-router detects it semantically and returns `project_notes_autosave: true`. The main agent delivers its primary answer, then asks the user whether to save — including a suggested category and slug. Only on approval are `project-notes/<category>/<slug>.md` and `project-notes/index.md` updated.

See `taskflow/agents/project-router.md` `Step 2b` for the detection conditions, and the "auto-save flow" section of `taskflow/prompts/notes_guidelines.md` for the save flow.

- Fires for: "investigate this repo's structure", "compare options A and B", "organize how X works"
- Does NOT fire for: questions / confirmations ("what is X?", "how does the auth flow work?"), debugging / troubleshooting, artifact-primary or trivial edits ("fix a typo in the README"), or explicit refusal ("don't save")

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
  ├─ [SessionStart:compact hook] (only on auto-compaction) ─→ resets injection flags so guidelines re-inject next turn
  │
  ├─ [UserPromptSubmit hook] ─→ creates state_file + parses pj: + injects session info / guidelines
  │
  ├─ [LLM] project determination (when a project is active; skipped when empty) ─→ writes the project name to state_file
  │
  ├─ [LLM] applicability decision ─→ decides whether progress management is needed
  │     not needed → run the task only
  │     needed     → read/write progress.md / tasks / project-notes
  │
  ├─ [LLM] project_notes_autosave judgement ─→ for investigation intents, prompts to save after the main response
  │
  ├─ task execution
  │     ├─ [PreToolUse:Write|Edit] writing a project-notes/ file ─→ injects the project-notes/index.md sync rule
  │     └─ [PostToolUse:Write|Edit|NotebookEdit|Bash] ─→ (a) rebuilds the progress.md table when a tasks/ file changed
  │                                                       (b) appends the written path(s) to the per-session .touched ledger
  │
  └─ [Stop hooks] ─→ archive plans/memory copies, AND
                     bind this session's work to each touched / owning task's @log,
                     delegating the summary + note↔task-link judgement to the async
                     progress-capture subagent (deterministic backstop if it is absent)
```

### hooks

Seven hook scripts run automatically when the plugin is enabled, wired in `hooks/hooks.json` across `UserPromptSubmit`, `PreToolUse`, `PostToolUse` (two hooks), `Stop` (two hooks), and `SessionStart:compact`. Two further files — `note_links.py` (the note↔task link data layer) and `log_lock.py` (a bounded per-task advisory lock) — are shared modules imported by the Stop hook, not wired hooks themselves.

#### UserPromptSubmit: session_init.py

Runs every turn. Manages `_projects/_state/{session_id}.json` and injects `[Progress Session]` into the LLM context. Creates `_projects/`, `_projects/_state/`, and a template `_projects/index.md` if missing.

Also handles guidelines injection: on the first turn of a session (and after compaction), the full content of `progress_guidelines.md`, `notes_guidelines.md`, and `tasks_guidelines.md` is injected. On subsequent turns, a reminder is injected to maintain attention to the guidelines at lower token cost — `guidelines_reminder.md` (default) or `guidelines_reminder_manifest.md` (`TASKFLOW_GUIDELINES_REMINDER=manifest`; see [Configuration](#taskflow_guidelines_reminder)).

##### Maintaining guidelines_reminder.md

`prompts/guidelines_reminder.md` is a keyword reminder injected every turn after the first. It works by re-activating the LLM's attention to the full guidelines injected earlier in the conversation. `prompts/guidelines_reminder_manifest.md` is the lower-cost variant selected by `TASKFLOW_GUIDELINES_REMINDER=manifest`: its PROHIBIT/FORMAT/AUTHORITY/NOTES/AUTOSAVE/TASK WRITE content is compressed to recall labels, but its ROUTER and RESPONSE LEADING LINES lines (the rules that must fire every turn unconditionally, not just when relevant) are kept as full text and MUST stay byte-identical to the same lines in `guidelines_reminder.md` — enforced by `tests/test_guidelines_reminder_mode.sh`.

**Design principle**: the reminder contains distinctive terms from the source guidelines — particularly prohibitions, format-specific patterns, and authority definitions — that boost attention weight on the corresponding full-text passages.

**Maintenance rule**: when any source that feeds the reminder is updated — the 3 guidelines (`progress_guidelines.md`, `notes_guidelines.md`, `tasks_guidelines.md`) plus `project_routing.md` (source of the ROUTER cue) — **both** `guidelines_reminder.md` and `guidelines_reminder_manifest.md` MUST be updated in the same commit. Stale keywords that reference removed rules cause hallucinated constraints; missing keywords for new rules cause silent non-compliance.

**Keyword selection criteria** (in priority order):

1. Prohibitions (what NOT to do) — highest violation risk when forgotten
2. Format-specific patterns (frontmatter fields, filename conventions, character limits)
3. Authority definitions (which source of truth governs which field)

##### Project rules (rules.md)

`session_init.py` also injects an optional per-project rules file, `_projects/<project>/rules.md`, when present. Rules are scoped to the taskflow project (`pj:`), not to a filesystem path — use `.claude/rules` for path/glob-scoped rules and `CLAUDE.md` for global rules.

- **On project switch**: the full body is injected once as a *primer*.
- **On subsequent turns**: a compact manifest of the file's `##` headings recurs as a recall cue (a "read-before-acting" trigger), keeping the rules warm at low token cost without re-injecting the full text.
- **`inject_every_turn: true`** (in the file's frontmatter): the full body is injected every turn instead — always warm, at a per-turn token cost.

The file is human-authored (no model-autonomous writes); the agent proposes a diff and applies only on user confirmation. `project_rules_indexed` in the state file gates the switch-time injection and is reset on compaction so the primer re-injects. See `_projects/harness-taskflow/project-notes/specs/project-rules-injection.md` (development-repo design notes; not shipped) for the full design.

#### PreToolUse: notes_index_reminder.py (matcher: Write|Edit)

Fires before a `Write`/`Edit` targeting a file under `_projects/<project>/project-notes/` (excluding `index.md` itself). Injects a `[Project Notes Index Rule]` reminder via `additionalContext`, instructing the LLM to keep `project-notes/index.md` in sync after the operation (add a row for new files, update on Description/Tags change, remove on delete).

#### PostToolUse: task_rebuild_progress.py (matcher: Write|Edit|Bash)

Fires after a `Write`/`Edit` targeting a file under `_projects/<project>/tasks/<status>/`, or a `Bash` command referencing such a path (e.g. the `mv` used by `/progress start` / `approve` / `unstart` — a rename alone never triggers `Write`/`Edit`). Runs `scripts/rebuild_progress.py` to regenerate the `progress.md` table region for that project, so the task index stays current without a manual `/progress rebuild`.

#### PostToolUse: touched_capture.py (matcher: Write|Edit|NotebookEdit|Bash)

Fires after every `Write` / `Edit` / `NotebookEdit` and file-touching `Bash` (`mv`/`cp`/`rm`, `>`/`>>` redirection, `tee`). Appends the normalized repo-relative write target(s) to a per-session `_projects/_state/{session_id}.touched` ledger (append-only, lock-free). This ledger is the input the Stop capture hook uses to decide which tasks this session actually touched — it observes *this session's tool writes* rather than scanning the jsonl or a git diff, which avoids mis-stamping unrelated tasks. Subagent / fork internal writes fire with the parent `session_id`, so they land in the parent's ledger automatically.

#### SessionStart: session_compact_reset.py (matcher: compact)

Fires when Claude Code auto-compacts the conversation. Compaction discards the `additionalContext` injected by `session_init.py`, so this hook resets the injection flags (`rules_loaded`, `indexed_project`, `guidelines_loaded`, `project_rules_indexed`) in the state file; all other fields are preserved. The next `UserPromptSubmit` turn then re-injects `static_rules`, the project index, the full guidelines, and the `rules.md` primer.

#### Stop: session_sync.py

Runs at session end. Copies plan/memory files modified within the last 10 minutes into the project directory.

#### Stop: session_progress_capture.py

Runs at session end alongside `session_sync.py`. It binds this session's work to each owning task's append-only `@log` block as a `- <ISO8601> [s:<sid>]: <summary>` line, using the `.touched` ledger (above) plus any `[tasks:]` exec-binding carry (below) to decide the owning tasks. Owner judgement — a one-line summary per touched task and note↔task links for freshly-written `project-notes/` deliverables — is delegated to the async `taskflow:progress-capture` subagent: the hook commits `capture.status=requested` and blocks once with an instruction to spawn it; the subagent writes a `{session_id}.capture` JSON sidecar; a later `Stop` applies it deterministically (`@log` summaries via `append_auto_binding`, note links via `append_note_link`). If no sidecar appears within a 15 s expiry, a deterministic backstop placeholder-binds every still-missing touched task instead. Round / lifecycle state lives in a `{session_id}.bind` sidecar (kept separate from the state JSON so concurrent rewrites cannot clobber it); the old `{session_id}.captured` marker is legacy and only swept by the 7-day cleanup. See `_projects/harness-taskflow/project-notes/specs/exec-binding.md` (development-repo design notes; not shipped with the plugin) and `project-notes/specs/note-task-link.md` for the design.

##### exec-binding (`[tasks:]` carry)

When a session does task work whose result lands **outside** the task's own `tasks/<status>/*.md` file (execution-by-reference — e.g. it reads a task or handoff and writes the result elsewhere), the agent lists the owning task filename(s) in a `[tasks: a.md b.md]` leading line of its reply. `session_progress_capture.py` reads this carry, union-merges it into the state `exec_bind` array, and binds each owning task's `@log`, so the work is recorded even though `tasks/` was never edited. Direct task-file edits need no `[tasks:]` — the PostToolUse `.touched` capture records them.

##### note↔task links (`@notes` block)

When a session writes a durable `project-notes/` deliverable, the progress-capture subagent maps it to its owning task and the hook records the link on the **task** side — an auto-managed `<!-- @notes:begin/end -->` block in the task file listing project-relative note paths (`note_links.py`). The link travels with the task file when the project directory is renamed or moved, and stale entries (a note whose file no longer exists) are skipped when the reverse index is built.

## Known issues

- **State file race condition**: Multiple hooks (`session_init.py`, `session_compact_reset.py`) read and write the same `_projects/_state/{session_id}.json` without file locking. In practice the triggering events (`UserPromptSubmit` vs `SessionStart:compact`) do not fire concurrently, so data loss has not been observed. A future release may add atomic writes or advisory locking. (Capture round-state and the touched ledger are deliberately kept in separate `.bind` / `.touched` / `.capture` sidecars to avoid this class of clobbering, and `@log` / `@notes` writes are serialized by the bounded advisory lock in `log_lock.py`.)
