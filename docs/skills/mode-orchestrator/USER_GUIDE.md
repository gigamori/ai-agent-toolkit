# mode-orchestrator — User Guide

User-facing guide for the `mode-orchestrator` skill: it reads a document that
already contains a **todolist** (a list of instructions) plus the context those
steps need, then runs each step as an isolated subagent turn tagged with a
role-mode `mode:` / `role:` header. The LLM-facing spec lives in this skill's
`SKILL.md` (under `skills/mode-orchestrator/`). For authoring workflow specs, see
`WORKFLOW_SPEC_AUTHORING.md` next to this file. (Japanese version:
`USER_GUIDE_ja.md`.)

## What it does

- Takes one document holding a todolist + related context.
- For each step it picks a **mode** (and optionally a **role**), assembles a
  prompt carrying that mode's NEVER/DO rules, and runs it as **one isolated
  general-purpose subagent turn**. One mode — and at most one role — per turn;
  they are never mixed.
- Only **autonomous** modes are executed. Interactive modes are surfaced as
  suggestions for you to run natively, not executed.
- Each turn writes a deliverable file; later turns receive earlier files by path.

It does **not** decompose an unstructured task from scratch — the todolist must
already be in the input.

## When to use it

Point the skill at a design doc, plan, or handoff that contains a todolist and
ask to orchestrate / run / execute its steps mode-by-mode. Typical trigger:

```
Use mode-orchestrator on path/to/plan.md
```

## Requirements — the Step 0 gate

Before generating anything, the skill gates the input and **rejects** it if:

- no identifiable list of instructions is present, or
- steps are too vague to map to a mode, or
- the context needed to carry out the steps is missing.

On rejection it names what is missing and asks you to supply a sufficient
todolist. It does not guess to fill gaps.

## Invoking it — flags

| Flag | Effect |
|---|---|
| _(none)_ | Present the turn plan and wait for your approval before executing. |
| `--auto` | Skip the approval gate; run all turns without per-plan confirmation. |
| `--roles` / `--roles=always` | Infer and attach a fitting role to every turn. Default: no inferred roles, but a role explicitly stated in the todolist is honored. |
| `--workflow=<name>` | Load a workflow spec (`workflows/<name>.md`) as defaults. A spec name declared inside the todolist is honored the same way. Default: no spec — run exactly as the todolist dictates. |

Flags use the `--` form on purpose — never `mode:` / `role:` colon-prefixes,
which the role-mode hook would capture.

## Modes

**Autonomous** (executed as a subagent turn): `survey`, `plan`, `execute`,
`debug`, `review`, `review-dev`. Aliases: `verify` → `debug`,
`implement` → `execute`.

**Interactive** (never executed — surfaced as a suggestion to run natively):
`ask`, `discuss`, `brainstorm`, `organize`. These need a live human exchange an
autonomous subagent cannot provide.

Mode is chosen hybrid: if a step names a mode it is honored; otherwise the fitting
mode is inferred from the step's content.

## Per-turn model

Each turn can run at a specific model. The model is resolved by precedence, with
no guessing from the mode alone:

1. **Per-step explicit** — a model named on the todolist step (or pinned by an
   active workflow spec for that step); the todolist wins on conflict.
2. **Spec table** — an active workflow spec's mode→model default.
3. **Inherit** — no override; the session model is used.

The turn plan shows each turn's model and which tier decided it.

## Failure recovery loop

When an `execute` turn runs a planned check (e.g. a test) that fails and the
failure looks fixable in-repo, it returns status `failed` and writes a
**Failure report** (Error / Reproduction / Error output / Target file(s) /
Context). The orchestrator then:

1. inserts a `debug` turn — diagnoses the root cause and proposes a minimal diff
   (it never applies the diff itself);
2. inserts a re-execute turn — applies that diff and re-runs the check;
3. if it now passes, the main sequence resumes; if not, it runs one more cycle.

The per-turn **cycle cap** is 2 by default (a workflow spec can override it). When
the cap is reached and the turn is still failing, it is escalated to `blocked` and
the run stops. A `debug` turn returning `needs-human` (e.g. the fix is out of the
task's authorized scope) also stops the run.

## Workflow specs

A workflow spec supplies **defaults and guidance** for one task type — a
recommended step sequence, a mode→model table, and the failure-policy cap —
without changing the engine. Specs are weakly coupled: the todolist is always
authoritative, and a mismatch between the todolist and the spec surfaces as a
**warning**, never a rejection.

The skill ships one spec, **`dev`** (`workflows/dev.md`), for development /
implementation work (investigate → design → review → build → test → review → sync
docs). Activate it with `--workflow=dev` or by naming it in the todolist. To add
a spec for another task type, see `WORKFLOW_SPEC_AUTHORING.md`.

## Run directory and artifacts

Each invocation creates one run directory in the workspace, e.g.
`mode-orchestrator-runs/<run-slug>/`:

- `NN-<mode>.md` — one deliverable per turn, in order. Recovery turns use the
  suffix form `NNa-debug.md` / `NNb-execute.md` / `NNc` / `NNd`.
- `index.md` — the turn plan (with each turn's model and tier), the spec warnings,
  the Failure policy, and each turn's status. It is an inspection index, not a
  resumable scheduler.

These are runtime artifacts — they are not committed.

## What it does not do

- No rollback / checkpoint / worktree around `execute` turns — working-tree safety
  is your git hygiene concern. Commit or stash WIP before an autonomous run.
- No mid-run interactive handoff and no resume scheduler.
- No parallel turns — turns run in order.
- No zero-decomposition — the todolist must be in the input.

Trust the run's **end state** (re-run the planned check), not a change-report's
claim about which file it edited; change-reports are self-attested.

## Related docs

- LLM-facing spec: `SKILL.md` (under `skills/mode-orchestrator/`).
- Authoring a workflow spec: `WORKFLOW_SPEC_AUTHORING.md` next to this file.
- The bundled `dev` spec: `skills/mode-orchestrator/workflows/dev.md`.
