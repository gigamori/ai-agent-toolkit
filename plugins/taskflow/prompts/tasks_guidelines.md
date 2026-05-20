## Overview

`_projects/<project>/tasks/` holds task-bound state. One file per task, status by folder.

```
tasks/
  0_todo/             Not started
  1_in_progress/      Started; work ongoing
  2_done/             Complete (human-approved)
```

Task tracking (status) and task context (body) are unified in a single file.

## File format

```markdown
---
priority: HIGH               # required: HIGH | MID | LOW
created: 2026-05-08          # required: YYYY-MM-DD
updated: 2026-05-12          # required: YYYY-MM-DD
dependencies: [task-id]      # optional: list of dependent task IDs (filename stems)
---

# <task title — becomes the H1 and progress.md row summary>

Body (mutable region — replace freely on each update).

Common subsections:
- ## Goal
- ## Context
- ## Plan / Next Steps

<!-- @log:begin -->
- 2026-05-08: started
- 2026-05-10: phase A complete
- 2026-05-12: phase B underway
<!-- @log:end -->
```

## Write rules

A task file has TWO regions:

1. **Body region** (from frontmatter close `---` to `<!-- @log:begin -->`): **mutable**. Replace fully when content changes.
2. **Log region** (`<!-- @log:begin -->` ↔ `<!-- @log:end -->`): **append-only**. Never edit, reorder, or delete existing entries.

When you modify the body or append to the log, you MUST also update `updated:` in frontmatter to today's date.

## Filename convention

- Pattern: `<YYYY-MM-DD>_<topic-slug>.md`
- `topic-slug`: kebab-case capturing intent in ≤ 50 chars. Non-ASCII (Japanese, CJK) is allowed.
- Collision: append `-N` starting from 2 (`-2`, `-3`, ...).

Example: `2026-05-13_extension-ui-context.md`, `2026-05-13_extension-ui-context-2.md`.

## Status transitions

| From | To | Trigger |
|---|---|---|
| `0_todo` | `1_in_progress` | `/progress start <id>`, or `mv` the file directly. |
| `1_in_progress` | `2_done` | **Human approval required**. `/progress approve <id>...`. |
| `1_in_progress` | `0_todo` | Send back / postpone. `/progress revert <id>`. |
| `2_done` | `1_in_progress` | Reopen completed task. `/progress revert <id>`. |

Folder location is the **single authority** for status. progress.md table is rebuilt from this via `/progress rebuild`.

## Coordination with progress.md

- progress.md TODO / In Progress / Completed tables are auto-generated from task files.
- Do NOT edit table rows inside `<!-- @table:begin --> ... <!-- @table:end -->` directly.
- To change a task's appearance in progress.md, edit the task file (frontmatter, H1, or folder), then run `/progress rebuild`.

## When to create a task file

- Work that takes more than one session
- Work the user explicitly asks to track
- Phase boundaries (explore → plan → implement); a task file collects context per phase

For one-shot work that completes within a single turn, no task file is needed.

## When to use `tasks/` vs `project-notes/`

| Concept | Location | Lifetime | Example |
|---|---|---|---|
| Active or completed task | `tasks/<status>/` | Task lifetime; `2_done/` keeps the historical record | "Implement ISSUE-005 Git diff preview" |
| Reusable project knowledge | `project-notes/<category>/` | Project lifetime | "Git diff preview design (specs/)" or "tree navigation TCs (checks/)" |

When a `2_done/` task produces durable knowledge (design decision, learned lesson, test spec), **distill** it into the appropriate `project-notes/<category>/` file. The task file stays in `2_done/` as the change-record.

## Prohibitions

- Do NOT add `status:` or `summary:` fields to task frontmatter. Folder name and H1 are authoritative.
- Do NOT add `category:` to task frontmatter (that's notes-only).
- Do NOT edit or reorder entries inside `<!-- @log -->`. Append only.
- Do NOT move a task to `tasks/2_done/` on your own judgment. Wait for explicit human approval (typically `/progress approve <id>`).
