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
