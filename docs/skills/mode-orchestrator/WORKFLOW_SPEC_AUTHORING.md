# Authoring a mode-orchestrator workflow spec

A workflow spec captures the defaults for one **task type** so the orchestrator can
supply a recommended step sequence, a mode→model table, and failure-policy
parameters without hardcoding any of it into the engine (`SKILL.md`). Adding a new
task type is a one-file change under `skills/mode-orchestrator/workflows/<name>.md`;
the engine reads it at runtime and needs no edit.

`workflows/<name>.md` is runtime-read (the engine loads it when `--workflow=<name>`
is active or the todolist names the spec), so it lives inside the skill dir. This
authoring contract is non-runtime and lives here under `docs/skills/`.

## Weak coupling — the governing principle

A spec supplies **defaults and warnings only**. The todolist is always
authoritative:

- The spec never rejects. Only the engine's Step 0 gate can reject a todolist.
- Where the todolist and the spec's recommended sequence disagree, the engine
  appends a **warning** to the turn plan and runs the todolist as written.
- A spec is optional; with none active the orchestrator runs exactly as the
  todolist dictates.

## Required sections

A spec file has these sections:

1. **Target task type** — one line naming the work the spec is for.
2. **Recommended step sequence** — an ordered table of `mode` + `model`
   (`(table)` to defer to the blanket table, or a pinned model) + task gloss. This
   is guidance for writing the todolist and the basis for the turn-plan mismatch
   warnings.
3. **mode→model defaults** — a blanket table mapping each mode to a default model.
   These are tier-2 defaults in the model precedence. Include `debug` here even
   though it never appears in the recommended step sequence: the engine's recovery
   loop spawns `debug` turns dynamically and resolves their model from this table
   (omit it and debug falls back to the inherited session model). The same applies
   to `review-dev`, which the decision loop spawns dynamically for the same
   reason — include it even if the recommended sequence never uses it.
4. **Failure policy** — the recovery cycle cap (engine default 2 if omitted) and,
   optionally, the decision insertion cap (engine default 2 if omitted). These
   two caps are the only failure/decision parameters a spec controls, and the
   engine counts them separately. Which outcomes reach which loop is fixed by the
   engine and not spec-tunable: only `failed` enters recovery and only
   `needs-decision` enters the decision loop, while `blocked` (including a
   permission denial), `needs-human`, and `aborted` (a turn that reported nothing
   — either a reply missing its `status:` final line, or one the watchdog ended
   with `TIMEOUT` / `STALL` — re-run once, then `needs-human`) bypass both. The
   `--decider` setting is likewise not a spec parameter: who adjudicates a fork
   is the operator's call for that run, not a property of the task type. Do not
   restate or attempt to override any of those in a spec. The watchdog's own
   thresholds are also not spec parameters: they are
   per-mode wall-clock values at the top of `scripts/watchdog.sh`. A spec that
   pinned them would let one task type silently widen the bound that keeps every
   run terminating, so tune them in the script, where the change is visible to
   every task type at once.
5. **Authoring guidance** — task-type-specific rules for writing a good todolist.

## Model precedence (how the spec's models are applied)

Highest to lowest:

1. **Per-step explicit model** — named on the todolist step, or pinned for that
   step in the recommended sequence; the todolist wins on conflict.
2. **mode→model table** — the spec's blanket default for that mode.
3. **Inherit** — no override; the session model is used.

Pin a model on a specific step (tier 1) when that one step needs a different tier
than its mode's blanket default — e.g. a documentation-consistency `survey` that
warrants `opus` while ordinary `survey` stays `sonnet`. Pinning keeps the blanket
table honest instead of raising the default for every step of that mode.

## Adding a new task type

1. Copy the section structure of an existing spec (`workflows/dev.md` is the
   reference implementation).
2. Fill the five sections for the new task type.
3. No engine edit is needed. Verify by generating a turn plan with
   `--workflow=<name>` against a representative todolist and confirming the model
   tiers, warnings, and Failure & decision policy block appear as intended.
