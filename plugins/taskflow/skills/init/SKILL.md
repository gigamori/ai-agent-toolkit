---
description: Initialize _projects/ directory structure in the current working directory for task and context management. Use when setting up a new project workspace.
---

# Initialize Taskflow

Create the `_projects/` directory structure in the current working directory.

## Base structure (always create)

1. `_projects/index.md` with the following template:

```markdown
| Project | Description | Target |
|---------|-------------|--------|
```

2. `_projects/_state/` directory

## Project structure (if $ARGUMENTS specifies a project name)

Create `_projects/<name>/` with:

1. `index.md` — project overview with this template:

```markdown
# <name>

<1–2 sentences describing the project's purpose>

## Target

- <path to the repository or code this project targets>

## Directory Semantics

- Default working directory: <first entry of Target>
- Project-management assets (project-notes / progress / handoff / project index) live under _projects/<name>/.

## Scope

- <the scope of tasks handled in this project>
```

2. `progress.md` — task tracking with this template:

```markdown
# <name> Progress

## TODO

| Priority | Task | Details | Prompt |
|----------|------|---------|--------|

## In Progress

## Completed

## Session Log
```

3. `project-notes/index.md` — project-notes index with this template:

```markdown
| File | Description | Tags |
|------|-------------|------|
```

4. `handoff/0_pending/` directory
5. `handoff/1_in_progress/` directory
6. `handoff/2_done/` directory

Also add the project to the `_projects/index.md` table.

## Cursor-compat hooks wiring (always run after base structure)

Cursor does not recognize the plugin structure, so we expand the plugin's `hooks/hooks.json` with absolute plugin paths and write the result into the CWD's `.claude/settings.json`. This is redundant for Claude Code users, but having the same settings in both places is harmless.

Steps:

1. Resolve the plugin's absolute path, in this order:
   - If the environment variable `$CLAUDE_PLUGIN_ROOT` is available, use it.
   - Otherwise, compute two levels up from this `SKILL.md` file (`.../taskflow/`).
   - If neither works, ask the user.

2. Read the plugin's `hooks/hooks.json` and substitute `${CLAUDE_PLUGIN_ROOT}` with the absolute path from step 1 to obtain the expanded JSON.

3. Read the CWD's `.claude/settings.json` (start from `{}` if it does not exist).

4. Merge into the `hooks` key:
   - If an array for the same event name already exists → append each entry from the plugin side to the end of the array. Skip entries whose `command` is already present (deduplication).
   - If the event name does not yet exist → add it.

5. Write `.claude/settings.json` back with 2-space indentation.

6. Tell the user:
   - When opening this workspace in Cursor, the user still needs to create the `.claude/agents/` and `.claude/skills/` symlinks by following the README.
   - If they are already present, this step is unnecessary.
