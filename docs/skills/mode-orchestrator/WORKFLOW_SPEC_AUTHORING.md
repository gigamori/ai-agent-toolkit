# Authoring a mode-orchestrator workflow spec

A workflow spec supplies guidance for one task type without owning shared model
routing. Runtime reads `workflows/<name>.md` only when `--workflow=<name>` is
active or the todolist names it.

## Weak coupling

The todolist is authoritative. A spec supplies defaults and warnings only:

- only the Step 0 gate rejects;
- a sequence mismatch is a turn-plan warning;
- no active spec means the todolist runs as written.

## Required sections

1. **Target task type** — one line naming the work.
2. **Recommended step sequence** — ordered `mode`, `effort` (`(infer)` or a pin), and task gloss.
3. **Failure policy** — recovery cap and optional llm-decision insertion cap.
4. **Authoring guidance** — task-type-specific todolist rules.

A spec may pin effort for one recommended step. It must not name models or define
mode→effort defaults: the shared profile maps effort to a harness model after the
step has been classified.

## Effort precedence

1. A todolist `model:` wins and bypasses effort.
2. Then todolist `effort:`, then a workflow step effort pin.
3. Otherwise the orchestrator infers `low`, `middle`, or `high`; use `middle` when unsure.
4. `references/execution-profiles.md` resolves the selected harness model.

The mapping is shared by workflow and workflow-less runs. A missing mapping is
`blocked`; it never falls back across efforts or to a harness default.

## Adding a task type

1. Add `workflows/<name>.md` with the four sections above.
2. Keep model routing out of it.
3. Generate a turn plan and confirm the sequence warning, effort sources, failure policy, and resolved overrides.
