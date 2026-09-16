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

Every variable below is read from the process environment. Export it in the shell, or — to set it permanently for Claude Code — put it in the `env` block of a `settings.json`: `~/.claude/settings.json` applies to every project, a workspace's `.claude/settings.json` or `.claude/settings.local.json` to that workspace only.

Two properties of the `settings.json` route matter before you rely on it (measured on Claude Code 2026-09-14 with `TASKFLOW_QMD_TIMEOUT`; the loader is not per-variable, but only that one was exercised):

- **A new value applies without restarting the session.** The next turn already sees it, hooks included. The qmd child inherits whatever the hook has, since `run_qmd` spawns it without an `env=` argument — that is the subprocess default read off the source, not a separately measured claim.
- **Deleting the key does NOT unset it in a running session.** The value survives in the session's environment until something overwrites it or the session restarts. In the measured case a deleted key stayed live for eight hours, across a byte-identical revert of the file it came from. Correct a bad value by writing the right one, not by removing the wrong one.

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

### `TASKFLOW_CONTEXT_DONE_ROWS_MAX`

Caps how many Completed rows enter an agent's **context**. `progress.md` itself always lists every task in `tasks/2_done/`; `scripts/view_progress.py` prints a truncated copy to stdout, and that copy is what the main agent should read when it needs more than the injected In Progress digest carries. The project router runs the script to judge but returns none of its output. Default: `10`. Set to `0` for unlimited. When rows are dropped, a `[context view]` footnote reports how many.

```json
{
  "env": {
    "TASKFLOW_CONTEXT_DONE_ROWS_MAX": "20"
  }
}
```

`scripts/view_progress.py --limit N` (or `--all`) overrides the environment variable for a single invocation.

> Retired in 0.2.6: `TASKFLOW_DONE_ROWS_MAX` and `rebuild_progress.py --done-rows-max`, which truncated `progress.md` on disk. Setting the old variable now has no effect — the file is no longer capped.

### `TASKFLOW_LOCK_TIMEOUT`

How long (in seconds) a writer waits to acquire the advisory write lock (`hooks/log_lock.py`) before giving up. Default: `3.0`. The acquire is **bounded**: on expiry the write proceeds *unlocked*, with a warning on stderr, rather than blocking — a stuck lock can never hang a session. Lowering it makes a contended write degrade to unlocked sooner.

```json
{
  "env": {
    "TASKFLOW_LOCK_TIMEOUT": "3.0"
  }
}
```

### `TASKFLOW_LOCK_STALE`

How old (in seconds) a lock sidecar must be before another writer may break it and take ownership. Default: `10.0`. The lock is "existence == lock" with no kernel-backed owner, so a sidecar left behind by a killed process is reclaimed only once it ages past this threshold — that bounded post-crash wait is the trade for having no orphaned-lock state to clean up. Normal holds are sub-millisecond; setting this below the longest expected hold risks a *live* holder's sidecar being broken.

```json
{
  "env": {
    "TASKFLOW_LOCK_STALE": "10"
  }
}
```

Both variables are read per call, and the Pi taskflow extension reads the same two names — set them identically on both sides, or the two harnesses will wait and expire on different schedules.

### `TASKFLOW_QMD_BIN`

Path of qmd's `bin/qmd` script, used by the per-turn project-notes push (see [project-notes](#project-notes)) when the project has a `project-notes/.qmd/` index. Unset (default) resolves the `qmd` on `PATH` and spawns the `node_modules/@tobilu/qmd/bin/qmd` next to it through `node` — the `.cmd` shim is never executed. A binary that cannot be resolved degrades the push to the lexical rank and is announced on every turn the degrade persists.

```json
{
  "env": {
    "TASKFLOW_QMD_BIN": "/path/to/node_modules/@tobilu/qmd/bin/qmd"
  }
}
```

### `TASKFLOW_NOTES_PUSH`

Which layers the per-turn project-notes push uses. `auto` (default): location (`anchors:` against the paths this turn named, read or wrote) + a qmd structured query when `project-notes/.qmd/` exists + the in-process lexical rank, merged as the union of both lists taken alternately. `lexical`: location + lexical only, qmd never spawned. `off`: location only.

```json
{
  "env": {
    "TASKFLOW_NOTES_PUSH": "lexical"
  }
}
```

### `TASKFLOW_QMD_TIMEOUT`

Seconds one qmd query of the per-turn project-notes push may take. Default: `6`. The query runs synchronously on the prompt-injection path every turn of a project that has a `project-notes/.qmd/` index — an accepted cost, measured at 2.7–2.9 s on the reference machine (GPU embedding, 79–210 notes) — so the budget exists to bound a cold or busy machine, not the normal case. On expiry the push falls back to the lexical rank for that turn and announces it on every turn it happens. A non-numeric or non-positive value falls back to the default. The Pi taskflow extension reads the same name — set it identically on both sides, but not by the same route: Pi's settings have no `env` block, so its extension takes the value from the process environment only. Setting it for both therefore means the OS-level user environment, and a process already running keeps the value it started with.

```json
{
  "env": {
    "TASKFLOW_QMD_TIMEOUT": "6"
  }
}
```

The cold case is not hypothetical. On the reference machine one query measured 8.83 s against 2.7–2.8 s for every other call in the same run (one observation) — it was the first call of a freshly started process, and what makes that call slow was not identified. A session started 16 s after its harness launched degraded its first turn and recovered on the next. Raise the value if losing the vector layer on turn 1 matters. The notice names the budget that expired — `qmd timeout after 6s` — so a timeout is distinguishable from the other degrade causes.

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
| `check` | Drift / stale / approval-pending detection, plus the notes data contract (flat layout, `summary:` ≤500 chars, `anchors:` resolving against the query-lsp snapshot at `<repo-root>/.lsp-cache/snapshot.json` or `--lsp-snapshot`). Always ends with `notes without summary: n of N`. Read-only. | `/progress check` |
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

> **Claude Code open links** (the **▶ CC** button and session / prompt launches) **require** the Claude Code VS Code / VSCodium extension (`anthropic.claude-code`). That extension is the only launch path: starting Claude Code in a terminal (CUI) is **not supported**, not even as a fallback. In serve mode the launcher CLI (`codium`, then `code`) is resolved and probed with `--list-extensions`. A determinate answer — extension present, extension absent, or no CLI on `PATH` — is cached for the life of the server, so the probe effectively runs once. A failed probe (exception, the 15 s timeout, or a non-zero exit) is **not** cached: it logs one line to stderr and is retried on the next click. Since a failed probe is no evidence that the extension is missing, the link is still launched optimistically and the page reports that the extension **could not be verified** rather than claiming it is not installed. Whenever the link was not launched (no CLI, or the extension confirmed absent) — or was launched without verification — the page carries a copyable fallback in a block of its own: `claude --resume <uuid>` for a session link (the bare UUID if the `claude` CLI is not on `PATH`), or the prompt body to paste for a ▶ CC / `/progress` link, since the CLI has no pre-filled-but-unsent prompt entry point.

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

`progress.md` is the task index. It has a free-text region (Architecture / Key Decisions / Open Issues / Reference Materials — human-edited) and an auto-generated table region (`<!-- @table:begin -->` ... `<!-- @table:end -->`) listing the TODO / In Progress / Completed tasks. Rebuild the table via `/progress rebuild`; never hand-edit inside the markers. The Completed section lists every task in `tasks/2_done/` — the file is never truncated. Only what reaches an agent's context is bounded, by `scripts/view_progress.py` — see [Configuration](#taskflow_context_done_rows_max).

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
- 2026-05-13 [s:abc123456789]: started
- 2026-05-14 [s:def678901234]: phase A complete | next: write tests
<!-- @log:end -->
```

- The body region is mutable; the log block is **append-only**.
- `## Next Steps` non-empty = pending; empty in `1_in_progress/` = completion candidate. The guidelines instruct the agent to maintain `## Next Steps` at the end of each turn that advances a task; `/progress audit` verifies it in code (see [How it works](#how-it-works)).
- Log lines carry a `[s:<session-id-tail-12>]` tag for downstream audit lookup.
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

`project-notes/index.md` is a 4-column table (`File | Description | Tags | Updated`) tracking notes; the LLM keeps it in sync (prompted by the PreToolUse hook) when notes are created or edited. A note's frontmatter may carry `summary:` (≤500 chars, read before opening the body) and `anchors:` (a list of repo-relative `path` or `path::symbol` entries naming the code the note is about); `/progress check` enforces both, and the layout is flat (`<category>/<name>.md`, sub-directories only under `_archive/`).

The project-router surfaces **no project-notes at all**. Note selection left the router when the deterministic per-turn push replaced it; the agent now reads nothing under `project-notes/` — not `index.md`, not a body file, not a directory listing — and returns no notes block, so it cannot summarize, translate, or confabulate note contents into the routing result. Notes reach the main agent through the injected `project-notes (index rows):` block described next, and through the `query-lsp` symbol route; the main agent reads note bodies itself.

Every turn, `session_init.py` also injects a `project-notes (index rows):` block — up to 8 verbatim `index.md` rows chosen by **location** (a note whose `anchors:` names a file this turn mentioned, read or wrote comes first; a note anchored in the same directory next, at most 4) and by **topic** (a qmd structured query over the note bodies when `project-notes/.qmd/` exists, unioned with an in-process lexical rank of Description + Tags by taking the two lists alternately), followed by `project-notes matched: M of N rows (K shown, S strong)`. `_archive/` rows are excluded and note bodies are never injected. `M` is the candidate count — the location matches unioned with every row the topic layer returned, qmd rows and lexical rows alike — so it can exceed the `K` rows actually shown without meaning that good candidates were cut. `S` is the narrower count: the location matches unioned with only the qmd rows that cleared a BM25 or vector-score floor, leaving the lexical tail out; with no qmd index, or while the vector layer is degraded, it is the location matches alone. `S` above `K` is the reading that strong candidates overflowed the block. The location signal comes from the `.reads` / `.touched` ledgers described under hooks; `TASKFLOW_NOTES_PUSH` and `TASKFLOW_QMD_BIN` are the knobs. Notes reach the agent by two routes and no others: this block, and — while it is editing or investigating a named symbol — enumerating that symbol's depth-1 callers and callees through the `query-lsp` skill (`incoming-calls` / `outgoing-calls`), keeping only the peers whose path is repo-relative, and grepping `project-notes/` for `::<name>` per peer. That route degrades to the block alone when `query-lsp` is unavailable, so a project without it loses recall but nothing else. The second route exists because the block matches `anchors:` at file and directory granularity, so a note anchored on a symbol in an untouched file cannot appear in it.

To enable the topic layer for a project, install the `@tobilu/qmd` npm package (Node ≥ 22), then from `_projects/<project>/project-notes/` run `qmd init`, `qmd collection add .`, add `ignore: ["index.md", "_archive/**"]` to the collection entry in `.qmd/index.yml`, and run `qmd embed` (GPU recommended; on a Vulkan GPU that aborts during embedding, `GGML_VK_DISABLE_COOPMAT=1` in the environment is the known fix). Keep `project-notes/.qmd/` out of version control if you track `_projects/`. Re-run `qmd update` and `qmd embed` after editing notes; until then the push still works over whatever is indexed.

#### Saving reusable knowledge, in the same turn

When a turn produced reusable project knowledge, the main agent writes it to `project-notes/<category>/<slug>.md` in that same turn and does not ask for confirmation. Nothing signals this: there is no router field and no approval step. The judgement is the agent's own, taken at the end of the turn against two sections of `taskflow/prompts/notes_guidelines.md` — `## What to save` decides whether the turn produced any, and `## Prohibitions` overrides the save whenever both apply, in which case the agent neither saves nor asks. `project-notes/index.md` is updated in the same turn.

See the `## Saving`, `## What to save` and `## Prohibitions` sections of `taskflow/prompts/notes_guidelines.md`.

- Saved: repository investigations, option comparisons, codebase-understanding memos, test specifications, runbooks, backlog candidates
- Not saved: anything derivable from code, single-session scratch memos, LLM-to-LLM handoffs (those go to `_projects/<project>/llm-handoff/`), and anything the user says not to save — a user instruction outranks this rule

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
    llm-handoff/              LLM-to-LLM handoff memos (flat, unindexed, transient)
    _archive/                 project-level archive
    plans/                    plan copies (auto-archived history)
    memory/                   memory copies (auto-archived history)
    .locks/                   @log / progress.md write locks (auto-managed; normally empty)
```

`.locks/` holds one short-lived sidecar per write target: one per task while
its `@log` / `@notes` block is being written (named after the task's file
name), plus `progress.md.lock` while `progress.md` itself is being rebuilt. It
is created on demand and each entry is deleted as soon as its last writer
finishes, so in a quiescent project the directory is empty. Never edit or
commit it.

The lock is advisory and **cross-harness**: the Pi taskflow extension derives
the same sidecar paths, so the two harnesses serialize against each other —
but only when both sides run protocol v2. It also does **not** cover a hand
edit or an LLM `Edit` tool call, which write at a layer that cannot take the
lock, so such an edit racing a hook write remains unprotected.

If entries do linger (a task was deleted while a lock existed, or a writer was
killed), `/progress check` reports them and
`scripts/clean_locks.py <project> --apply` clears them. A crash-orphaned
sidecar whose target still exists — including `progress.md.lock`, which both
of those deliberately never flag — is instead reclaimed by the next writer
once it ages past `TASKFLOW_LOCK_STALE`; unlike a `flock`-style lock, nothing
is released automatically when a process is killed.

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
  ├─ [LLM] end-of-turn save judgement ─→ reusable knowledge produced this turn is written to project-notes/ in the same turn, no confirmation
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

Nine hook scripts run automatically when the plugin is enabled, wired in `hooks/hooks.json` across `UserPromptSubmit`, `PreToolUse`, `PostToolUse` (three hooks), `Stop` (two hooks), `SessionStart:compact`, and `PreCompact`. `notes_push.py` (the per-turn project-notes push) is a further shared module imported by `session_init.py`, not a wired hook. Two further files — `note_links.py` (the note↔task link data layer) and `log_lock.py` (the bounded advisory write lock, protocol v2) — are shared modules, not wired hooks themselves: `note_links.py` is imported by the Stop hook, and `log_lock.py` by the Stop hook and by `scripts/rebuild_progress.py`, which takes the same lock around its `progress.md` read→write window.

**Where `_projects/` is resolved.** Every hook that needs the state tree walks up from the session's launch directory to the nearest ancestor that holds `_projects/_state` — the launch directory itself counts — and falls back to the launch directory when no ancestor qualifies. This matters because a hook process keeps the directory the session was **launched** in for the session's whole life (a `cd` in a Bash tool call does not move it): starting Claude Code inside a subdirectory of your workspace previously made the plugin no-op for that entire session, and now joins the workspace's existing tree instead. The one deliberate exception is the Stop hook's 7-day stale-marker cleanup, whose target stays pinned to the launch directory — a subdirectory-launched session simply skips that housekeeping, and the next session started at the workspace root performs it.

#### UserPromptSubmit: session_init.py

Runs every turn. Manages `_projects/_state/{session_id}.json` and injects `[Progress Session]` into the LLM context. Creates `_projects/`, `_projects/_state/`, and a template `_projects/index.md` if missing.

On an ordinary turn it also injects a `[Project Progress: <project>]` block through `hooks/progress_digest.py`: the `## In Progress` table header, separator and up to `IN_PROGRESS_ROWS_MAX` (5) data rows sliced out of `progress.md`, closed by `in-progress: <shown> of <total> rows shown`. It is absent on the turns that carry `project_routing.md` or the full guidelines, because those payloads already exceed the harness's inline-delivery budget on their own and the digest would arrive truncated; it is absent on `pj:?` discovery turns; and it is replaced by the single line `[Project Progress] In Progress digest omitted (context budget)` when the whole assembled payload would otherwise cross that budget. Anything the digest cut — further In Progress rows, the TODO or Completed tables — the agent gets by running `scripts/view_progress.py`.

It also builds the per-turn `project-notes (index rows):` push block (see [project-notes](#project-notes)) through `hooks/notes_push.py`: location rows from the note `anchors:` matched against the paths this turn names, read (`.reads` ledger) or wrote (`.touched` ledger), then topic rows from a qmd structured query — spawned as `node <bin/qmd> query … --no-rerank --json` with a timeout of `TASKFLOW_QMD_TIMEOUT` seconds (default 6), only when the project has a `project-notes/.qmd/` index — fused with the in-process lexical rank. When qmd is expected but cannot run, the topic layer is the lexical rank alone and a `[project-notes] vector layer unavailable: <reason>` line is injected on **every** turn the degrade persists. It used to be injected once per session; that was measured to hide a four-day outage across several sessions, because the usual cause — a process whose ancestry predates the environment registration qmd needs — does not clear on its own and one notice scrolls away. The reason travels with each line, so a change of cause is visible too.

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

#### PreToolUse: notes_scan_gate.py (matcher: Grep)

Denies a `Grep` whose `path` lies inside a `project-notes/` tree (`_archive/` included) and whose `pattern` does not contain `::`. That shape is the corpus scan the note rule forbids — the symbol route's step 3 greps `::<name>` per peer, and nothing else may enumerate the corpus. The denial is a `permissionDecision: "deny"` carrying the rule as its reason, so a blocked agent is told what is allowed instead of retrying blind. Scope: this closes the `Grep` tool only. `Read` is never denied, because a note reached legitimately (via the block or a `::` Grep) is opened with `Read` and the hook cannot tell that file's provenance; `Glob`→`Read` and a `grep`/`rg`/`cat` run through `Bash` both pass. A subagent's own calls do **not** pass — they are denied under the parent's registration, the same way `touched_capture.py` already sees subagent writes. The guarantee is "a Grep-tool corpus scan is impossible", not "a corpus scan is impossible". One deployment note: the matcher set is fixed when a session starts, so this hook does not take effect in a session that was already running when it landed.

#### PostToolUse: task_rebuild_progress.py (matcher: Write|Edit|Bash)

Fires after a `Write`/`Edit` targeting a file under `_projects/<project>/tasks/<status>/`, or a `Bash` command referencing such a path (e.g. the `mv` used by `/progress start` / `approve` / `unstart` — a rename alone never triggers `Write`/`Edit`). Runs `scripts/rebuild_progress.py` to regenerate the `progress.md` table region for that project, so the task index stays current without a manual `/progress rebuild`.

#### PostToolUse: touched_capture.py (matcher: Write|Edit|NotebookEdit|Bash)

Fires after every `Write` / `Edit` / `NotebookEdit` and file-touching `Bash` (`mv`/`cp`/`rm`, `>`/`>>` redirection, `tee`, and an in-place `sed -i`/`--in-place`). Appends the normalized repo-relative write target(s) to a per-session `_projects/_state/{session_id}.touched` ledger (append-only, lock-free). This ledger is the input the Stop capture hook uses to decide which tasks this session actually touched — it observes *this session's tool writes* rather than scanning the jsonl or a git diff, which avoids mis-stamping unrelated tasks. Subagent / fork internal writes fire with the parent `session_id`, so they land in the parent's ledger automatically — with two exclusions. The first follows from exactly that: a write under `_projects/_state/` is dropped, so the capture subagent's own judgment sidecar never appears in the ledger it feeds. The second is the workspace's own `tmp/`: that tree is scratch, and a mode-orchestrator run would otherwise bury the round's source edits under hundreds of its own artifact paths (a nested `sub/tmp/` is ordinary work and stays recorded). Heredoc bodies are stripped before the command is parsed, so text inside one is never mistaken for a redirection (a body's own writes are not recorded either, which is the long-standing accepted capture gap for `python -c open()` as well; `sed -i`/`--in-place` outside a heredoc body is recognized as of 2026-08-20, skipping any operand containing an unexpanded shell metacharacter such as `$f`); an unterminated heredoc falls back to parsing the command as written.

#### PostToolUse: read_capture.py (matcher: Read)

Fires after every `Read`. Appends the read file's repo-relative path to a per-session `_projects/_state/{session_id}.reads` ledger, truncated to its last 200 lines on every append. It is deliberately a separate file from `.touched`: that ledger's raw line count is the Stop hook's round cursor, so recording reads there would open a round on a read-only turn. The `.reads` tail — together with the `.touched` tail and the paths named in the prompt — is the location signal of the project-notes push; the same exclusions apply (`_projects/_state/`, `tmp/`, paths outside the state tree).

#### PreCompact: precompact_flush.py

Fires just before Claude Code summarizes the conversation (manual `/compact` and auto-compaction alike — the entry carries no matcher). Compaction is not a special mechanism here: this hook is a **second call site of the same round computation the Stop hook uses**. It reads the `.touched` ledger slice for the round in flight, drops tasks the agent already logged itself, and — if anything is still unwritten — appends `(auto) unflushed at compaction; summary pending (r<N>)` to each of those tasks' `@log` and prints one plain-text line asking the summarizer to preserve that per-task progress verbatim. `N` is the round the next `Stop` will commit, so two compactions inside one round collapse to a single line.

When nothing is unflushed the hook prints **nothing at all** and exits — the common case stays silent, which also keeps Claude Code's precomputed-compaction reuse intact. It never writes the `{session_id}.bind` sidecar (the `Stop` hook is its sole writer), so the cursor does not move and the Stop that follows a compaction still forms its round normally.

#### SessionStart: session_compact_reset.py (matcher: compact)

Fires when Claude Code auto-compacts the conversation. Compaction discards the `additionalContext` injected by `session_init.py`, so this hook resets the injection flags (`rules_loaded`, `indexed_project`, `guidelines_loaded`, `project_rules_indexed`) in the state file; all other fields are preserved. The next `UserPromptSubmit` turn then re-injects `static_rules`, the project index, the full guidelines, and the `rules.md` primer.

#### Stop: session_sync.py

Runs at session end. Copies plan/memory files modified within the last 10 minutes into the project directory.

#### Stop: session_progress_capture.py

Runs at session end alongside `session_sync.py`. It binds this session's work to each owning task's append-only `@log` block as a `- <ISO8601> [s:<sid>]: <summary>` line, using the `.touched` ledger (above) plus any `[tasks:]` exec-binding carry (below) to decide the owning tasks.

Binding is per **round**, not per session. The `.touched` ledger is append-only with one line per write event, so the hook keeps a cursor into it: each `Stop` looks only at the slice written since the last committed round, and that slice defines the round's active task set — tasks written directly, the owning tasks of any `project-notes/` deliverable written in it (resolved through the reverse index), and this turn's `[tasks:]` carry. A task the agent already logged itself in that round is dropped from the set, so a turn that follows the guidelines spawns nothing at all; the hook is the safety net for the rounds that would otherwise go unrecorded. A long session therefore accumulates one `@log` line per round per task instead of a single line for its whole lifetime.

Task resolution is **not** confined to the session's own project. Each ledger line carries its own `_projects/<project>/` prefix, so a task the session edited in another project of the same repository is resolved and bound under *that* project's root — previously such writes were dropped in silence. A name is only accepted as a project when `_projects/<name>/tasks/` exists, which is what keeps the `_projects/_state/` sidecar directory from ever being treated as one. Tasks are consequently identified as `<project>/<basename>` in the round state, in the capture subagent's context (which also receives a `project_roots` map) and in the hook's report lines.

Owner judgement — a one-line summary per active task and note↔task links for freshly-written `project-notes/` deliverables — is delegated to the async `taskflow:progress-capture` subagent: the hook commits `capture.status=requested` (freezing the round's task set) and blocks once with an instruction to spawn it; the subagent writes a `{session_id}.r{N}.capture` JSON sidecar (`N` = the round it was spawned for, stamped into the name by the hook); a later `Stop` applies it deterministically (`@log` summaries via `append_auto_binding`, note links via `append_note_link`). If no sidecar appears within a 30 s expiry, a deterministic backstop placeholder-binds every task of that round that has not produced a line yet — with two exceptions, at different granularities. At the round level, a round opened **solely** by a `[tasks:]` carry, whose own slice holds neither a task write nor a `project-notes/` write, is skipped wholesale, because "touched" would be false of a round that touched nothing (a round that mixes a carry with real slice writes is not such a round and is placeholder-bound as before). Per task, the placeholder step also skips a task currently filed under `tasks/2_done/` (read live off the current index at backstop time); the `referenced` note-owner over-bind that runs before the placeholder step is unaffected by a task's status folder and still binds a `2_done` task normally. A real summary still applies to a carry-only round normally if its sidecar lands. A subagent that finishes *after* that expiry is not wasted, and it does not have to beat the next round either: the `.bind` keeps the frozen task/note set of the last 3 rounds, so a sidecar is always checked against the set of the round named in its own file name and still applies its summary and its note links on top of the placeholder. Only the current round's sidecar closes the lifecycle; a sidecar naming a round older than the retained window is discarded unapplied and reported once as `round-mismatch` — an unreadable (torn / non-JSON) sidecar gets the same disposal once its round ages out of the window, and is silently retried while still inside it. Placeholder lines carry an `(r<N>)` round tag, which is also the idempotency key — re-applying an identical entry never duplicates a line. The subagent has one further judgement to make: when a task's `## Next Steps` section has stood unchanged across the rounds whose summaries kept landing on it, the hook lists that task in the request's context block and asks for a proposed replacement section. The hook never writes such a proposal into the task file — it is folded into the same Stop block already used for genesis-task suggestions (no new notification channel), rendered under its own "Proposed Next Steps rewrites" heading with a `(next-steps)` prefix so it reads distinctly from a genesis `(proposed)` task suggestion in that block, for you to apply or ignore, and at most once per task per session. Round / lifecycle state (cursor, round number, per-task line counts, the last 3 rounds' task/note sets, and the per-task `## Next Steps` digest and staleness counter behind that proposal) lives in a `{session_id}.bind` sidecar (kept separate from the state JSON so concurrent rewrites cannot clobber it); the old `{session_id}.captured` marker is legacy and only swept by the 7-day cleanup. See `_projects/harness-taskflow/project-notes/specs/exec-binding.md` (development-repo design notes; not shipped with the plugin) and `project-notes/specs/note-task-link.md` for the design.

##### exec-binding (`[tasks:]` carry)

When a session does task work whose result lands **outside** the task's own `tasks/<status>/*.md` file (execution-by-reference — e.g. it reads a task or handoff and writes the result elsewhere), the agent lists the owning task filename(s) in a `[tasks: a.md b.md]` leading line of its reply. `session_progress_capture.py` reads this carry, union-merges it into the state `exec_bind` array, and binds each owning task's `@log`, so the work is recorded even though `tasks/` was never edited. Direct task-file edits need no `[tasks:]` — the PostToolUse `.touched` capture records them. If the name does not match any task md in the current project, the hook says so once (`exec-skip(unresolved)`) rather than silently ignoring it — check the spelling, and note that the carry resolves in the active project only.

##### note↔task links (`@notes` block)

When a session writes a durable `project-notes/` deliverable, the progress-capture subagent maps it to its owning task and the hook records the link on the **task** side — an auto-managed `<!-- @notes:begin/end -->` block in the task file listing project-relative note paths (`note_links.py`). The link travels with the task file when the project directory is renamed or moved, and stale entries (a note whose file no longer exists) are skipped when the reverse index is built.

## Known issues

- **State file race condition**: Multiple hooks (`session_init.py`, `session_compact_reset.py`) read and write the same `_projects/_state/{session_id}.json` without file locking. In practice the triggering events (`UserPromptSubmit` vs `SessionStart:compact`) do not fire concurrently, so data loss has not been observed. A future release may add atomic writes or advisory locking. (Capture round-state and the touched ledger are deliberately kept in separate `.bind` / `.touched` / `.capture` sidecars to avoid this class of clobbering, and `@log` / `@notes` writes are serialized by the bounded advisory lock in `log_lock.py`.)
