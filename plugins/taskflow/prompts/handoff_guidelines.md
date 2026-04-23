## Overview

`_projects/<project>/handoff/` is the directory that holds task-bound context.
It pairs with the concise instruction in the `Prompt` column of `progress.md` to store the detailed information the AI should consult.

## Three-state lifecycle

```
handoff/
  0_pending/        not started (created but not yet consumed)
  1_in_progress/    in progress (consumed by AI; work is ongoing)
  2_done/           done (human-approved)
```

### Transition rules

| Transition | Trigger | Actor |
|---|---|---|
| 0_pending → 1_in_progress | Subagent consumes at session start | AI (automatic) |
| 1_in_progress → 2_done | Human approves completion | Human instruction → LLM executes |
| 1_in_progress → 0_pending | Human sends back | Human instruction → LLM executes |

Transitioning to `2_done` requires explicit human approval. The AI MUST NOT move it on its own judgment.

## Coordination with progress.md

- handoff is used together with the instruction in the `progress.md` TODO `Prompt` column.
- Split them: instruction (the concise directive) goes into `progress.md`; context (the details) goes into handoff.
- Link them by filename. In the `Prompt` column, reference with the folder omitted: `@handoff/<filename>` (because the folder changes across transitions).
- When the status in `progress.md` changes, the handoff folder moves in lockstep (see `progress_guidelines.md` for details).

## Writing out a handoff

Write a file into `handoff/0_pending/` in any of these cases:

- The next session moves to a different phase (explore → plan, plan → implement, etc.).
- The outputs of the current session must be passed as-is into the next session.
- The user explicitly requested a handoff.

Filename: `<date>_<topic>.md` (e.g. `2026-04-18_repo-survey.md`).

## Content

As context for the AI in the next session, include:

- What was done in the previous session (briefly)
- The context that is required (investigation results, rationale for decisions, file paths, constraints, etc.)
- Concrete procedure or approach, if any

## When to use handoff/ vs project-notes/

- `handoff/`: task-lifetime. Concrete context for a specific task. Moves to `2_done/` when the task is complete.
- `project-notes/`: project-lifetime. General knowledge referenced across the whole project.
