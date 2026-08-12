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

**One prerequisite if you launch it headlessly** (`claude -p`, a wrapper
script, a nested run): before the gate, the skill runs one small shell command
to work out which harness it is on. On Claude Code that command needs
permission, and an interactive session usually grants it on the spot — a
headless one cannot ask, so it is refused and the run stops there. Allow that
one command in the settings the run reads, or pass it via `--allowedTools`;
`references/harness-cc.md` carries the exact string. Nothing to do for an
ordinary interactive run.

## Invoking it — flags

| Flag | Effect |
|---|---|
| _(none)_ | Present the turn plan and wait for your approval before executing. |
| `--auto` | Skip the approval gate; run all turns without per-plan confirmation. |
| `--roles` / `--roles=always` | Infer and attach a fitting role to every turn. Default: no inferred roles, but a role explicitly stated in the todolist is honored. |
| `--workflow=<name>` | Load a workflow spec (`workflows/<name>.md`) as defaults. A spec name declared inside the todolist is honored the same way. Default: no spec — run exactly as the todolist dictates. |
| `--decider=llm` | Delegate `needs-decision` forks to an inserted turn that decides. Default `human` — the run pauses at the step boundary and shows you the question. See the decision loop below. |

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
3. **Inherit** — the turn adds no model of its own, so it runs on whatever the
   harness uses for an un-overridden delegation. On Claude Code that is your
   session's model. On Pi it is **pi's own configured default, which need not
   be a Claude model** (measured), so pin a model per step or via a spec table
   there rather than relying on inherit.

The run index records both the tier that decided and the model override
actually passed (or `none`) — without the second, an index can claim `inherit`
beside a call that named a model, and nothing later can tell whether a spec
table took effect.

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

### What does *not* enter the recovery loop

Only `failed` — "the work ran, a check didn't pass, and it looks fixable here" —
is worth diagnosing. Four other outcomes deliberately bypass the loop:

- **`blocked`** — including any turn whose tool call the **permission system
  denied**. A denial is not an in-repo bug: re-running the turn hits the same
  wall every cycle, so the run stops and asks you instead. The turn cannot
  waive this itself: even a denial it judged inessential — one it worked
  around and still finished the task — must end as `blocked`, never `ok`.
  On Claude Code this is also machine-checked: after each turn's status line
  the orchestrator runs `scripts/deny_scan.sh` against the turn's transcript,
  and a detected denial overrides a self-reported `ok`.
- **`needs-human`** — the turn needs a decision only you can make.
- **`needs-decision`** — the turn hit a fork, not a failure. It goes to the
  decision loop below instead.
- **`aborted`** — the turn said *nothing* about the task. Either its reply
  arrived without the required status line — or with a perfectly good one
  in the wrong place, since the last line is the anchor and reading a status
  off-anchor would let a truncated reply pass as a finished one (interrupted, killed, or
  off-contract), or it never replied at all and the **turn watchdog** ended it
  (see below). There is no failure to diagnose, so the orchestrator re-runs that
  turn **once** and, if it is still unreadable, stops with `needs-human`. This
  re-run does not consume the cycle cap.

Each turn ends its reply with a fixed final line — `status: <...>; file: <path>`
— and the orchestrator reads the outcome from that line alone. That is why a
turn that omits it is treated as `aborted` rather than guessed at: a silent or
broken turn fails loudly instead of passing as success.

**Only `execute` turns are even offered `failed`.** The status is defined as "a
planned check did not pass", and running such a check is what an `execute` turn
does; every other mode gets `ok` / `blocked` / `needs-human` / `needs-decision`.
If some other turn returns `failed` anyway, it is out of contract — the run
stops as `needs-human` rather than starting a recovery loop that would have no
Failure report to diagnose.

## Decision loop

A turn can also stop on a **fork** rather than a failure: an ambiguity it is
forbidden to resolve on its own, two architectures with no clear winner, a
review finding with no single defensible recommendation. Without somewhere to
put that, such a turn's only exit is `needs-human`, which ends the whole run.

It returns status `needs-decision` and writes a **Decision request** into its
deliverable, with four fields:

| Field | Content |
|---|---|
| Question | What must be decided, in one sentence. |
| Options | Two or more, each with its trade-off. |
| Impact on remaining steps | `none`, or the revised remaining sequence. |
| Work state | `complete` — the deliverable stands and the turn is only flagging the fork — or `stopped` — it could not finish without the answer. |

An incomplete request is not treated as a decision: it is read as `needs-human`
and the run stops. That is deliberate — the four fields are what separates a
real fork from a turn handing its job back.

What happens next depends on `--decider`:

- **`llm`** — an extra `review-dev` turn is inserted to adjudicate. It
  gets the deliverable of the turn that raised the fork (which may itself be an
  inserted debug turn), the plan if there is one, and **the input document**
  (what a fork should be decided *toward* is the run's purpose, which lives only
  there), and must record exactly one option with the reason. It may pick an
  option the request did not list, saying so. It must *refuse* to decide
  anything irreversible or outward-facing, or anything that changes what the run
  is for — those come back to you as `needs-human`.
- **`human` (default)** — no turn is inserted. The run stops at the step boundary and
  shows you the question and the deliverable path; your answer is written into
  the decision record and the run continues. In a non-interactive run
  (`claude -p` and the like) nothing can answer, so the run simply ends, with
  the request preserved on disk for you to restart from.

Then the run continues one of two ways, read off the `Work state` field rather
than guessed: **`complete`** with a listed option → the deliverable stands and
the decision record is added to the following turns' inputs; **`stopped`**, or
an unlisted option was adopted → the originating turn is re-run with the
decision record as an extra input.

If the decision carries an **amendment**, only the not-yet-run part of the turn
plan is regenerated — finished turns are untouched — and the revised remainder
must still pass the Step 0 gate. The original plan stays in the run index, so
the drift from what you approved stays visible.

The per-turn **decision cap** is 2 insertions (a workflow spec can override it).
It counts *inserted* turns, so it applies to `--decider=llm` only: when you are
the decider nothing is inserted and nothing is capped, and the same turn may
bring you a third and a fourth fork. The cap exists to stop an unattended run
from adjudicating in circles, which a run that needs your answer every round
cannot do — capping it would only mean your third legitimate fork comes back as
"cap reached" instead of being asked.
At the cap the run stops with `needs-human`, not `blocked`: two rounds that fail
to converge means the judgement is overloaded, and what is missing is your
judgement, not a capability. The decision cap and the recovery cycle cap are
counted separately, and both are charged to the turn that originated them — a
turn that owns a lettered sequence carries both counts, and **every turn added
to that sequence — a re-run, an inserted debug turn, an inserted decision turn
— spends from it rather than opening its own**. Without that a loop could mint
fresh budget every round simply by inserting a turn.

A fork can also be raised by an *inserted* turn — a debug turn that finds
several candidate fixes, say. When that happens and the continuation is form
(b), what gets re-run is the turn that raised the fork (the debug turn), not
the original one; and that re-run consumes no recovery cycle — the recovery cap
counts failed cycles, and a fork is not a failure. What the round spent, if
anything, is a decision insertion, and only under `--decider=llm`.

`--auto` does **not** override `--decider=human`: it skips only the initial
turn-plan approval, so a human-decided run still waits at its forks.

A permission denial is never a decision. A turn that reports `needs-decision`
after being denied is corrected to `blocked` by the same machine check that
catches a denied turn reporting `ok` — otherwise a wall would consume a decision
round, or worse, get adjudicated around.

## Turn watchdog

The status line only classifies turns that answer. A subagent can also stop
answering entirely — killed, interrupted, or simply hung — and no notification
ever arrives. The orchestrator cannot notice that on its own: it only acts when
something wakes it, so an answer that never comes is a wait that never ends.

So every turn is started alongside a **watchdog** (`scripts/watchdog.sh`) running
in the background. It reports whichever of these happens first, then exits — and
that exit is what wakes the orchestrator:

- the turn's **deliverable file** is written during the turn and is non-empty — `DONE`;
- the turn's **wall-clock deadline** passes with no deliverable — `TIMEOUT`;
- the subagent's transcript **stops growing** for the stall threshold — `STALL`.

"During the turn" is load-bearing: a re-run writes to the same path as the
attempt it replaces, so the file is often already there when the second attempt
starts. The watchdog therefore requires the deliverable to be newer than its own
start. Without that it would report `DONE` immediately against the dead
attempt's leftovers and the re-run would go unwatched — silently, since a
premature `DONE` looks exactly like a real one.

The watchdog only reports; it never stops anything itself. On `TIMEOUT` or
`STALL` the orchestrator stops the turn and classifies it `aborted`, then re-runs
it once — exactly as it would an unreadable reply. Nothing is diagnosed, because
a turn that never reported gives nothing to diagnose.

Defaults are at the top of the script — edit them there:

| Setting | Default |
|---|---|
| Stall threshold | 600s |
| Deadline, `survey` | 600s |
| Deadline, `plan` | 2400s |
| Deadline, `execute` | 1500s |
| Deadline, every other mode | 900s |
| Poll interval | 15s |

They are deliberately generous. A long tool call looks exactly like a hang from
outside — the transcript is idle either way — so the stall threshold is set well
above the longest tool call a turn is expected to make. That detects a real hang
late rather than cutting a working turn short. If a run keeps aborting turns that
were still making progress, the budget is too tight; the run index records which
check fired, so you can see that and raise it.

The watchdog bounds time, not correctness. It can tell you a turn ran too long;
it cannot tell you a turn did the wrong thing.

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

- `NN-<mode>.md` — one deliverable per turn, in order. Any turn inserted for
  turn `NN` — by either loop — takes the next free suffix letter, so a turn that
  goes through both reads as `05a-decision.md` → `05b-execute.md` →
  `05c-debug.md` → `05d-execute.md`. The mode is in the filename, so which loop
  produced which artifact is never ambiguous.
- `index.md` — the turn plan (with each turn's model: the deciding tier and the override actually passed, or `none`), the spec warnings,
  the Failure & decision policy, and each turn's status. Also recorded: each
  turn's decision insertions and which continuation form was taken, any
  amendment (alongside the original plan it replaced), any `--decider=human`
  wait, and any turn re-run after an `aborted` reply along with which check
  caught it — a missing status line, or the watchdog's `TIMEOUT` or `STALL`. An
  aborted turn writes no deliverable, so this is the only place that records it
  happened. It is an inspection index, not a resumable scheduler.

These are runtime artifacts — they are not committed.

## What it does not do

- No rollback / checkpoint / worktree around `execute` turns — working-tree safety
  is your git hygiene concern. Commit or stash WIP before an autonomous run.
- No mid-**step** interruption and no resume scheduler. A `--decider=human` wait
  is not an exception: it happens at a step boundary, where every artifact is
  already written.
- No parallel turns — turns run in order.
- No zero-decomposition — the todolist must be in the input.

Trust the run's **end state** (re-run the planned check), not a change-report's
claim about which file it edited; change-reports are self-attested.

## Related docs

- LLM-facing spec: `SKILL.md` (under `skills/mode-orchestrator/`).
- Authoring a workflow spec: `WORKFLOW_SPEC_AUTHORING.md` next to this file.
- The bundled `dev` spec: `skills/mode-orchestrator/workflows/dev.md`.
