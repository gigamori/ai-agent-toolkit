## Loading

- Always read `progress.md` at the start of work.
- progress.md is split into two regions:
  - **Free-text sections** (Architecture / Key Decisions & Policies / Open Issues / Reference Materials): hand-editable; LLM and human both edit.
  - **Table region** (between `<!-- @table:begin -->` and `<!-- @table:end -->`): auto-generated from `tasks/<status>/*.md` files. Do NOT hand-edit content inside this block.
  - The Completed section is capped to the most recent rows (`TASKFLOW_DONE_ROWS_MAX`, default 10); a footnote reports the omitted count when capped. Full history always remains in `tasks/2_done/`.

## Single authority

| Field | Source of truth | Editor |
|---|---|---|
| Task status (TODO / In Progress / Completed) | Folder of the task file (`tasks/0_todo/`, `1_in_progress/`, `2_done/`) | `/progress` commands (`start` / `approve` / `unstart`) or `mv` |
| Task summary text | Task file H1 line (`# <title>`) | Edit in the task file body |
| Task priority | `priority:` in task frontmatter | Edit frontmatter |
| Task created / updated date | Task frontmatter `created:` / `updated:` | Edit frontmatter |
| Architecture / Key Decisions / Open Issues / Reference Materials | progress.md section content | Hand-edit |

The table region is a **cache** rebuilt from task files. It is never authoritative.

## `/progress` sub-actions

| Sub-action | Effect |
|---|---|
| `/progress check` | Run drift / stale / approval-pending detection. Read-only. |
| `/progress sync` | Alias of rebuild — regenerate the table region from task files. |
| `/progress rebuild` | Regenerate the table region from current task files. |
| `/progress start <id>...` | Move tasks from `tasks/0_todo/` to `tasks/1_in_progress/`. Multiple IDs OK. Also reopens from `tasks/2_done/`. |
| `/progress approve <id>...` | Move tasks from `tasks/1_in_progress/` to `tasks/2_done/`. Multiple IDs OK. |
| `/progress unstart <id>` | Move a task back to `tasks/0_todo/` (from `1_in_progress/`; from `2_done/` it is a non-adjacent jump confirmed with a ⚠). |

## Status transitions

```
0_todo/  →  1_in_progress/  →  2_done/
        ←                  ←
```

Folder location is the **single authority**. Move the task file, then either run `/progress rebuild` to refresh progress.md, or let the next `/progress check` flag the drift.

Entering `2_done/` requires explicit human approval (typically via `/progress approve`). AI does NOT auto-move to `2_done/`.

## When to write

- **New task**: create task file with frontmatter + H1 + body. Default folder is `tasks/0_todo/`. If the task is clearly already underway, create in `tasks/1_in_progress/`. When ambiguous, ask the user which folder to use. See [tasks_guidelines.md].
- **Starting a task**: `/progress start <id>`, or `mv tasks/0_todo/<file> tasks/1_in_progress/`, then update `updated:` in frontmatter.
- **Recording progress mid-task**: append a line to the `<!-- @log:begin --> ... <!-- @log:end -->` block in the task file.
- **Editing task content**: rewrite the body region (between frontmatter end and `<!-- @log:begin -->`).
- **Policy decision**: append to `## Key Decisions & Policies` in progress.md.
- **Surfacing a problem**: append to `## Open Issues` in progress.md.
- **New reference material**: append to `## Reference Materials` in progress.md.
- **Architectural change**: update `## Architecture` in progress.md.

After advancing a task, keep `## Next Steps` current — it must reflect the remaining work at the end of the turn.

There is no `Session Log` section. Per-task history lives in each task file's `<!-- @log -->` block.

## Prohibitions

- Do NOT edit content inside `<!-- @table:begin --> ... <!-- @table:end -->` by hand. Run `/progress rebuild` instead.
- Do NOT create multiple table regions in progress.md. There is exactly **one** `<!-- @table:begin/end -->` block per project, which rebuilds all three status tables (TODO / In Progress / Completed) together.
- Do NOT add `status:` or `summary:` fields to task frontmatter. Folder name and H1 are the sole sources of truth.
- Do NOT add `## Session Log` or `## Last Updated` sections to progress.md. Both are obsolete in v0.2.2.
- Do NOT add `## Completed Tasks` or other duplicate task sections. Task status is derived solely from folder location.
- Do NOT delete entries from a task's `<!-- @log -->` block. Append only.
- Do NOT move a task into `tasks/2_done/` on your own judgment. Wait for explicit human approval.
