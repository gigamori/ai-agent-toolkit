## Loading

- Always read `progress.md` at the start of work.

## TODO `Prompt` column

- The TODO table includes a `Prompt` column. Record a ready-to-use prompt that a human can paste into the LLM to execute the task.
- Form: `pj:<project> <instruction>` — it must be copy-pasteable as-is, including the project specifier.
- Match the granularity of the task. One TODO row → one prompt.
- When referencing a handoff file, omit the folder: use `@handoff/<filename>` (do NOT include `0_pending/` etc., because folder transitions would break the path).
- When referring to project-notes, include `@project-notes/<filename>` in the prompt.
- If there are prerequisites or prior work, include them in the prompt (e.g. "after the build, ...").

## Session Log

- The Session Log is a history record. Do NOT use it to convey instructions.
- `Next steps` is a record of "what remains", not an instruction to the next session.
- Use `handoff` to convey concrete instructions to the next session.

## Status transitions and handoff coordination

When changing a status in `progress.md`, consult the `@handoff/<filename>` in the `Prompt` column and move the corresponding handoff file in lockstep:

- TODO → In Progress: `handoff/0_pending/<filename>` → `handoff/1_in_progress/<filename>`
- In Progress → Completed: after explicit human approval, `handoff/1_in_progress/<filename>` → `handoff/2_done/<filename>`
- In Progress → TODO (send back): `handoff/1_in_progress/<filename>` → `handoff/0_pending/<filename>`

`[HOLD]` marker: attaching `[HOLD]` to an In Progress entry suppresses the subagent's completion reminder. Record the reason for the hold.

## When to write

- Structural changes: update Architecture
- Starting a task: update In Progress (and run the handoff-coordination move)
- Policy decisions: append to Key Decisions & Policies
- Problems or concerns surfacing: append to Open Issues
- New reference material: append to Reference Materials
- Task completion: move to Completed and remove from In Progress (after human approval, run the handoff-coordination move)
- End of work: append a Session Log entry in this form and update Last Updated
  ### [Date] - [Title]
  - Goal:
  - Done:
  - Next steps:

## Prohibitions

- Do NOT delete or rewrite existing records. Only append or update status.
- Do NOT move a partially completed task to Completed. Leave it in In Progress and record the progress in the Status column.
