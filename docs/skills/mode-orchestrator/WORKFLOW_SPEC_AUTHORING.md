# Authoring a mode-orchestrator workflow spec

A workflow spec supplies task-type guidance without owning shared model routing.
Runtime reads `workflows/<name>.md` only when `--workflow=<name>` is active or
the todolist names it.

## Weak coupling

The todolist is authoritative. A spec supplies guidance and failure-policy caps:

- only the Step 0 gate rejects a missing or insufficient todolist;
- a sequence mismatch is a turn-plan warning;
- no active spec means the todolist runs as written;
- an invalid explicit `(workflow-step: ID)` is a setup fault and blocks before
  plan approval or delegation.

## Required sections

1. **Target task type** — one line naming the work.
2. **Recommended step sequence** — an ordered table with stable `id`, `mode`,
   `effort`, and `task` columns.
3. **Failure policy** — recovery cap and optional llm-decision insertion cap.
4. **Authoring guidance** — task-type-specific todolist rules.

Each effort cell is exactly `(infer)`, `basic`, `pro`, or `ultra`. A spec must
not name models or establish a general effort default keyed by mode. The shared
profile resolves a selected effort to the harness model only after final-turn
classification or an explicit bound pin.

## Turn-facing policy (optional)

A spec may carry sections beyond the four above that a turn must follow, as
`research` does with its Turn policy. The orchestrator reads a spec only for its
sequence, effort pins and failure-policy caps, and never injects spec text into a
turn's prompt. Such a section therefore reaches a turn only when the todolist
passes the spec file, or an artifact holding the section, to that turn as an
input. A spec that relies on this says so in its Authoring guidance, and has
each todolist step name its step id in its text so the turn knows which sections
apply to it.

## Binding and precedence

A numbered todolist step may carry at most one `(model: VALUE)`, one
`(effort: basic|pro|ultra)`, and one `(workflow-step: ID)` anywhere on its first
physical line after the ordinal. Metadata on continuation lines is ordinary task
text. Empty values, duplicate keys, invalid effort, an ID absent from the
active workflow, and duplicate workflow row IDs are setup faults. Recognized metadata is removed before the
instruction is classified and applies to every turn split from that numbered step.

A workflow effort pin applies only when exactly one row's `id` is named by
`(workflow-step: ID)`. A bound `(infer)` row does not pin effort: the final turn
is classified and records `inferred-effort`. An unbound row is guidance only;
position, matching mode, and semantic similarity never create a binding.

1. `(effort: VALUE)` uses source `step-effort`.
2. A bound pinned workflow row uses source `workflow-effort`.
3. A bound `(infer)` row or no binding uses source `inferred-effort`.
4. The model request is adopted separately, by priority: the execution-time
   prompt (a step's `(model: VALUE)` is one, normalized from the input
   document) → the environment's child-agent model setting → the mandatory
   default, `references/execution-profiles.md`, which resolves a selected
   effort to the exact harness model. A resolved model makes the turn's
   effort `null`.

A missing or malformed profile, missing harness cell, or
invalid workflow pin is `blocked`. It never uses a harness default or an effort
fallback, and a higher-priority source never hides a broken profile.

## Adding a task type

1. Add `workflows/<name>.md` with the four sections above.
2. Give every recommended-sequence row a stable unique `id` and a valid effort
   cell; duplicate IDs are a blocking invalid spec. Keep model routing out of the spec.
3. Generate a turn plan and confirm explicit bindings, inferred rows, effort
   sources, model-request sources, failure policy, and planned dispatches.
