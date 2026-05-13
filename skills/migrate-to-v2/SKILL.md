---
name: migrate-to-v2
description: Migrate one taskflow project from v1 (progress.md + handoff/) to v2 (tasks/ + categorized project-notes). Invoke as `/migrate-to-v2 <project-name>`. Does not run in a forked subagent — runs in the main session so the user can supervise.
disable-model-invocation: true
arguments: [project_name]
allowed-tools: Bash(uv run python *) Bash(mkdir *) Bash(mv *) Bash(cp *) Bash(ls *) Read Write Edit
---

# Migrate `$project_name` from taskflow v1 to v2

## Prerequisites

- Manual backup of `_projects/<project>/` is recommended before running. No rollback is provided.
- The **taskflow plugin must be installed** (provides `rebuild_progress.py` and `check_progress.py` used in Phases 7-8).

## Project location

Look in both standard roots, use whichever has `_projects/$project_name/`:

- `_projects/$project_name/`
- `<secondary-projects-root>/$project_name/`

If neither exists, abort and report.

## Script paths

- **This skill's own helper** (legacy progress.md parser): `${CLAUDE_SKILL_DIR}/scripts/parse_progress_table.py`
- **taskflow plugin scripts** (used in Phases 7-8): `${CLAUDE_PLUGIN_ROOT}/scripts/`
  - `rebuild_progress.py` — regenerate progress.md table region
  - `check_progress.py` — final verification

## Phase 1: Extract v1 structured data

```bash
uv run python ${CLAUDE_SKILL_DIR}/scripts/parse_progress_table.py \
  <project>/progress.md > c:/tmp/migrate_${project_name}.json
```

Read the JSON. It contains four arrays:

- `todo` — rows from `## TODO` (columns vary; usually Priority/Task/Details/Prompt)
- `in_progress` — rows from `## In Progress`
- `completed` — rows from `## Completed`
- `session_log_headers` — list of `### YYYY-MM-DD - <title>` lines

**Important**: parse_progress_table.py only handles **markdown-table** format. If a section uses bullet lists (e.g., `## Completed` as `- item1\n- item2`) instead of a table, the script returns an empty array for that section. In that case, fall back to `Read`ing the section directly from progress.md with `offset`/`limit`, extract entries by hand, and merge into the `task_entry` plan in Phase 2. Document this fallback in the final report.

## Phase 2: Plan task units

Build a list of `task_entry` dicts from the three table arrays. For each row:

| Field | Source |
|---|---|
| `status` | `0_todo` / `1_in_progress` / `2_done` per source section |
| `priority` | `row.Priority` if present, else `MID` |
| `date` | `row.Date` if Completed; else today |
| `title` | `row.Task` (raw, becomes H1) |
| `topic_slug` | slugify(title) — rules in [helpers.md] |
| `filename` | `<date>_<topic_slug>.md`, collision suffix `-2`, `-3`, … |
| `body` | `row.Notes` (Completed) or `row.Status` (In Progress) or `row.Details` (TODO) |
| `quick_start` | `row.Prompt` if TODO and present |

Then list `<project>/handoff/0_pending/`, `1_in_progress/`, `2_done/` (whichever exist). For each handoff file:

- If filename's date+slug substantially overlaps with a planned task entry, **merge** handoff body into that task's `body` field
- Otherwise, create a new `task_entry` with status from the subfolder and body from the handoff file

Show the user a brief plan summary before Phase 3 (count tasks per status, list any handoff orphans). **Do not auto-continue** if there are unexpected orphans — wait for user confirmation.

## Phase 3: Map Session Log entries to tasks

Read session_log_headers from Phase 1 JSON. For each entry:

1. Extract date (YYYY-MM-DD) and title (after the dash)
2. Match to a `task_entry` using heuristics from [helpers.md Session-log mapping]
3. Unmatchable → group `[unassigned]`

For each match, fetch the entry body from `<project>/progress.md` using `Read` with `offset`/`limit` (lines between this `###` header and the next `###` header).

Output a mapping `{task_id: [log_body_chunks]}`.

## Phase 4: Write task files

For each planned task:

```bash
mkdir -p <project>/tasks/<status>/
```

Write `<project>/tasks/<status>/<filename>` with the following structure:

```
---
priority: <PRIORITY>
created: <date>
updated: <date>
---

# <title>

<body content>

## Quick start  ← only if `quick_start` is set
<quick_start>

<!-- @log:begin -->
- YYYY-MM-DD: <one-line summary derived from log body>
- ...
<!-- @log:end -->
```

If a planned filename collides on disk, append `-2`, `-3`, … per the convention.

## Phase 5: Archive handoff/

```bash
mkdir -p <project>/_archive
mv <project>/handoff <project>/_archive/handoff-pre-v2
```

Skip if `<project>/handoff/` does not exist.

## Phase 6: Categorize project-notes/

For each `<project>/project-notes/*.md` and `<project>/project-notes/<subdir>/*.md` (excluding `index.md` and items already under the target category names):

1. Determine target category using heuristics from [helpers.md Category map]
2. `mkdir -p <project>/project-notes/<category>/`
3. `mv <file> <project>/project-notes/<category>/`
4. If the file has no YAML frontmatter, prepend the minimal block:
   ```
   ---
   domain: development
   created: <YYYY-MM-DD from stat mtime>
   updated: <YYYY-MM-DD>
   ---
   ```
   (Edit the file via Read + Write to prepend.)

Then rebuild `project-notes/index.md` to the 4-column format:

```
| File | Description | Tags | Updated |
|------|-------------|------|---------|
| specs/api-design.md | <existing description, truncated to 100 chars> | <existing tags> | <YYYY-MM-DD> |
```

Preserve existing Description / Tags from the old index.md where possible. File path now includes the category prefix (e.g., `specs/api-design.md`).

Report each `mv` as 1 line. Flag ambiguous categorizations explicitly.

## Phase 7: Archive legacy progress.md, then rebuild

```bash
cp <project>/progress.md <project>/_archive/progress-pre-v2.md
```

Then edit `<project>/progress.md` in place:

- **Remove** these sections entirely: `## Completed`, `## In Progress`, `## TODO`, `## Session Log`, `## Last Updated`
- **Keep** these sections: `## Architecture`, `## Key Decisions & Policies`, `## Open Issues`, `## Reference Materials`, and any free-text sections (project-level notes that aren't table-tracked)
- **Drop** the H1 line if it duplicates the project name (we'll let rebuild set it)

Then:

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_progress.py <project>
```

This re-creates the H1 + scaffold (if missing) and appends the `<!-- @table:begin --> ... <!-- @table:end -->` block with the new task tables.

## Phase 8: Verify

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/check_progress.py <project>
```

- Exit 0 → migration is clean, proceed to final report
- Exit 1 → list findings; classify as "needs manual fix" vs "expected legacy state"

## Final report

Print a concise summary (≤ 30 lines):

```
Migration of <project> complete.

Phase 2: tasks identified
  TODO:        <n>
  In Progress: <m>
  Completed:   <k>
  Handoff merges: <j>
  Handoff orphans (new task): <x>

Phase 3: session log mapping
  Assigned: <a>
  Unassigned: <u>

Phase 6: notes categorization
  specs:          <s>
  investigations: <i>
  checks:         <c>
  procedures:     <p>
  backlog:        <b>
  _archive:       <r>
  Ambiguous (review): <q>

Phase 8: check_progress findings
  drift / violation / stale / pending: <breakdown>

Manual follow-up:
- <line per unassigned log / ambiguous note / unresolved finding>
```

Stop after this report. Do not start the next project migration in the same session.

---

Detailed rules (slugify, category heuristics, log mapping similarity) are in [helpers.md](helpers.md). Read helpers.md once at the start of Phase 2.
