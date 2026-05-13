---
name: progress
description: Inspect, rebuild, sync, approve, or revert taskflow project task progress. Sub-actions — check, sync, rebuild, approve, revert. Invoke as `/progress <action> [project] [ids...]`. Runs in the main session.
disable-model-invocation: true
allowed-tools: Bash(uv run python *) Bash(mv *) Bash(mkdir *) Bash(ls *) Bash(cat *) Bash(stat *) Read Write Edit
---

# /progress

Arguments: `$ARGUMENTS`

Execute the following procedure exactly. Report each step's outcome to the user.

## Step 1 — Parse arguments

`$ARGUMENTS` is a whitespace-separated list. Token 0 = action. Token 1 = optional project name. Remaining tokens = IDs.

Valid actions: `check`, `sync`, `rebuild`, `approve`, `revert`.

If token 0 is not in the valid list, reply with usage:

```
Usage: /progress <check|sync|rebuild|approve|revert> [project] [ids...]
```

and stop.

## Step 2 — Resolve the project

If token 1 is provided AND does NOT match `YYYY-MM-DD_`, treat it as the project name. Otherwise resolve via:

1. List `_projects/_state/*.json` sorted by mtime descending.
2. Read the most recent file.
3. Use its `project` field (JSON).

If the resolved project is empty or the state file does not exist, reply:

```
no project; pass /progress <action> <project> [ids...]
```

and stop.

Then locate the project root by trying these directories (use the first that exists):

- `_projects/<project>/`
- `<secondary-projects-root>/<project>/`

If neither exists, reply `project '<name>' not found` and stop.

## Step 3 — Dispatch on action

Plugin script paths:

- `${CLAUDE_PLUGIN_ROOT}/scripts/check_progress.py`
- `${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_progress.py`

### action = `check`

Run:

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/check_progress.py "<project-root>"
```

Read stdout. If empty / `OK: no drift...`, reply `OK: <project>` and stop. Otherwise, summarize findings in ≤ 20 lines:

```
project: <name>
findings: <total> (drift=<n>, violation=<n>, stale=<n>, pending=<n>)

  [check_name] <path>
    <message>
  ...

next: /progress rebuild  (for drift) | /progress approve <id>  (for pending)
```

### action = `sync` — alias of `rebuild`. Proceed to rebuild.

### action = `rebuild`

Run:

```bash
uv run python ${CLAUDE_PLUGIN_ROOT}/scripts/rebuild_progress.py "<project-root>"
```

Echo stdout (it shows TODO / In Progress / Completed counts).

### action = `approve`

For each ID in tokens 2..N (filename-prefix match against `<project-root>/tasks/1_in_progress/`):

1. Resolve the file. If 0 or 2+ matches, report and skip.
2. `mkdir -p "<project-root>/tasks/2_done"` (idempotent).
3. `mv "<project-root>/tasks/1_in_progress/<file>" "<project-root>/tasks/2_done/<file>"`.
4. Edit the moved file's frontmatter `updated:` to today (YYYY-MM-DD). Append one line to its `<!-- @log:begin --> ... <!-- @log:end -->` block: `- <today>: approved → 2_done`.

After all IDs, run `rebuild_progress.py` and report.

### action = `revert`

For each ID in tokens 2..N (filename-prefix match):

1. Locate which folder under `<project-root>/tasks/` contains the file.
2. Compute target:
   - From `1_in_progress` → `0_todo`
   - From `2_done` → `1_in_progress`
   - From `0_todo` → error "cannot revert from 0_todo"
3. `mkdir -p` the target folder (idempotent).
4. `mv` the file.
5. Edit frontmatter `updated:` to today; append `<!-- @log -->` line: `- <today>: reverted to <target>`.

After all IDs, run `rebuild_progress.py` and report.

## Output rules

- Echo each Bash command's relevant output (or a 1-line summary).
- For destructive actions (`approve`, `revert`), explicitly list each moved file.
- Total response ≤ 30 lines.

## Restrictions

- Do NOT edit task body or H1 — only `updated:` frontmatter + one `<!-- @log -->` line per action.
- Do NOT modify `progress.md` directly; only via `rebuild_progress.py`.
- Do NOT touch files outside `<project-root>/tasks/`.
