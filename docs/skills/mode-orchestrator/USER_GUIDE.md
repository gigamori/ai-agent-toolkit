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
- Each turn has one responsibility: it picks a **mode** (and optionally a
  **role**), assembles a prompt carrying that mode's NEVER/DO rules, and runs
  as **one isolated general-purpose subagent turn**. A step is split when its
  mode, role, model, authority, or deliverable changes; multiple files or tool
  calls alone do not split it. One mode — and at most one role — per turn; they
  are never mixed.
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
`references/harness-cc.md` carries the exact string. That is not always
enough: under `--permission-prompts none`, the turn watchdog's launch command
(`exec bash …/watchdog.sh …`) was refused even with an exact allow rule (Claude
Code 2.1.286, 2026-10-02, one run), and the run then stops `blocked` at its first
turn; other permission modes were not tried. On Claude Code the restart gate's
scripts also run on `node`: without it, an aborted turn is never run again.
Nothing to do for an ordinary interactive run.

## Invoking it — flags

| Flag | Effect |
|---|---|
| _(none)_ | Present the turn plan and wait for your approval before executing. |
| `--auto` | Skip the approval gate; run all turns without per-plan confirmation — for what the plan already pins. A model resolution newly derived at run time (natural language, abbreviation, alias, prefix, series) still needs your explicit approval; headless, the run stops before starting and shows it. |
| `--roles` / `--roles=always` | Infer and attach a fitting role to every turn. Default: no inferred roles, but a role explicitly stated in the todolist is honored. |
| `--workflow=<name>` | Load a workflow spec (`workflows/<name>.md`) as task-type guidance. A spec name declared inside the todolist is honored the same way. Default: no spec — run exactly as the todolist dictates. |
| `--decider=llm` | Delegate `needs-decision` forks to an inserted turn that decides. Default `human` — the run pauses at the step boundary and shows you the question, except a fork between instructions a model wrote, which an inserted turn decides under either value. See the decision loop below. |

Flags use the `--` form on purpose — never `mode:` / `role:` colon-prefixes,
which the role-mode hook would capture.

## Modes

**Autonomous** (executed as a subagent turn): `survey`, `plan`, `execute`,
`debug`, `review-process`, `review-dev`, `review-doc`. Aliases: `verify` → `debug`,
`implement` → `execute`.

**Interactive** (never executed — surfaced as a suggestion to run natively):
`ask`, `discuss`, `brainstorm`, `organize`, `explain`. These need a live human
exchange an autonomous subagent cannot provide.

Mode is chosen hybrid: if a step names a mode it is honored; otherwise the fitting
mode is inferred from the step's content.

## Per-turn effort and model

`references/execution-profiles.md` maps `basic`, `pro`, and `ultra` to one model
per harness. It is the mandatory skill-default binding — the last of three
model-request sources (see the delegation contract's model-request sources).
The runs run under a permission gate: the orchestrator reads what the host
publishes about delegation permission before any turn is delegated, and
`deny` / unobservable stops the run before delegating.

A numbered todolist step may carry at most one `(model: VALUE)`, one
`(effort: basic|pro|ultra)`, and one `(workflow-step: ID)` anywhere on its first
physical line after the ordinal. Continuation lines are task text. Empty values,
duplicate keys, an invalid effort, an ID absent from the active workflow block,
or a duplicate active workflow ID block the run before approval or delegation. Recognized metadata is removed before the
remaining instruction is classified, and copied to every turn split from that step.
Write separate numbered steps when split responsibilities need different explicit
values.

Effort comes first, from:

1. `(effort: VALUE)` — source `step-effort`.
2. `(workflow-step: ID)` selects exactly one matching active workflow row only. A
   duplicate ID blocks. A pinned `basic`, `pro`, or `ultra` cell uses source `workflow-effort`; `(infer)` leaves
   the final instruction to classification with source `inferred-effort`.
3. Without an explicit effort or bound pin, the orchestrator classifies
   the final instruction as `basic`, `pro`, or `ultra`; use `pro` when unsure.

The model request is adopted separately, by priority: the execution-time
prompt (whole-run, per-step, or per-effort wording — a per-step `(model:
VALUE)` is such a request, normalized from the input document you designated
for the run) → the environment's child-agent model setting (only an absent or
unset setting falls through; an invalid or unreadable one stops the run
instead of being silently replaced) → the skill-default profile, source
`step-model` when the request came from step metadata. A resolved model makes
the turn's effort `null`. Quotations in the document, past-run records and
artifact mentions are not requests. The plan shows, per turn, where the model
request came from, its original wording and scope, and what it resolved to.

A workflow row never binds by sequence position, matching mode, or semantic
similarity. Inserted debug and llm-decision turns alone use the policy-selected
`ultra` effort with source `policy-effort`. A missing or malformed profile, mapping,
or pin stops as `blocked`; there is no harness-default or cross-effort fallback,
and a broken profile is never hidden by a higher source.

Under `--auto`, the plan runs without per-turn confirmation only while every
model resolution it uses is an exact match of what you specified. A model
resolved at run time from natural language, an abbreviation, an alias, a
prefix, or a series always needs your explicit approval of scope and target —
headless, the run stops before starting and shows you the resolution.

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

- **`blocked`** — on Pi's CLI route, a prompt-file write failure, read failure
  or empty message stops before the CLI starts. It is not retried and enters
  neither the abort restart gate nor recovery; the run index records the
  path and diagnostic, not a delegation attempt that never happened.
  This also includes any turn whose tool call the **permission system
  denied**. A denial is not an in-repo bug: re-running the turn hits the same
  wall every cycle, so the run stops and asks you instead. The turn cannot
  waive this itself: even a denial it judged inessential — one it finished
  the task without — must end as `blocked`, never `ok`. Nor may it
  reproduce the denied call's effect another way (after a refused
  `git stash`, reverting the files with `cp` or an edit); a different,
  safer action is not a reproduction.
  On Claude Code this is also machine-checked: after each turn's status line
  the orchestrator runs `scripts/deny_scan.sh` against the turn's transcript
  and those of any agents the turn started, and a detected denial overrides a
  self-reported `ok` (on a turn the orchestrator itself stopped it is
  `needs-human` instead: the stop can leave a trace that reads like a refusal).
  The check counts every
  refused call — a permission rule, a plugin's hook, the auto-mode classifier
  or its being unreachable, a permission prompt that was not approved, or an
  interrupted call — and keeps its verdict in the run directory, one file per
  attempt (`05-1-deny.txt` for turn `05`'s first attempt).
- **`needs-human`** — the turn needs a specific input, judgment, or action
  only you can supply. Its reply includes `Reason: <specific observed blocker>`
  and `Human request: <exact question to answer or action to take>` before
  the final status line; it does not send you to the deliverable for these.
  Complexity or unfinished work alone is not a human request.
- **`needs-decision`** — the turn hit a fork, not a failure. It goes to the
  decision loop below instead.
- **`aborted`** — the turn said *nothing* about the task. Either its reply
  arrived without the required status line — or with a perfectly good one
  in the wrong place, since the last line is the anchor and reading a status
  off-anchor would let a truncated reply pass as a finished one (interrupted, killed, or
  off-contract), or it never replied at all and the **turn watchdog** ended it
  (see below). There is no failure to diagnose, so the orchestrator does not
  diagnose it. It runs that turn again **once** at most, and only through the
  **restart gate**: the stop must be confirmed, the denial check (Claude Code)
  clean, and a judge — one fixed `ultra` delegation, not a turn — must find in
  the attempt's evidence that running the turn again from the start cannot
  duplicate an effect it already had. Otherwise the run stops with `needs-human`
  at the first abort. A re-run does not consume the cycle cap.

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
| Question | What must be decided, naming where each side of the fork comes from: the turn's `Task:` line (the step's own words), its `Part:` line (the orchestrator's note on which share of a split step the turn does), an input by path, or a standing rule. |
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
- **`human` (default)** — the orchestrator first looks up, in the run index,
  where each side named in the Question comes from. If every side is an
  instruction a model wrote during this run — another turn's deliverable, or the
  `Part:` line of a split step — you could not know that contradiction, so a
  `review-dev` turn is inserted to decide it exactly as under `llm` (it also
  counts against the decision cap below). Otherwise no turn is inserted: the run
  stops at the step boundary and shows you the question and the deliverable
  path; your answer is written into the decision record and the run continues.
  If the inserted turn finds it may not decide — a side rests on something a
  human wrote, or one of the grounds above applies — the fork comes to you the
  same way. In a non-interactive run (`claude -p` and the like) nothing can
  answer a fork shown to you, so the run simply ends, with the request preserved
  on disk for you to restart from.

Then the run continues one of two ways, read off the `Work state` field rather
than guessed: **`complete`** with a listed option → the deliverable stands and
the decision evidence is added to the following turns' inputs; **`stopped`**, or
an unlisted option was adopted → the raising turn is re-run. Its parent is the
inserted decision turn where one decided, and the human decision record where
you did. In both cases it retains the raising turn's model routing.

If the decision carries an **amendment**, only the not-yet-run part of the turn
plan is regenerated — finished turns are untouched — and the revised remainder
must still pass the Step 0 gate. The original plan stays in the run index, so
the drift from what you approved stays visible.

The per-turn **decision cap** is 2 insertions (a workflow spec can override it).
It counts *inserted* turns: every fork under `--decider=llm`, and under `human`
every fork routed to an inserted turn. A fork you answer inserts nothing and is
never capped, and the same turn may bring you a third and a fourth fork. The cap exists to stop an unattended run
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
anything, is a decision insertion — under `--decider=llm`, or under `human` when
the fork was routed to an inserted turn.

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
in the background. It wakes the orchestrator only when something has gone wrong,
reporting whichever comes first and then exiting — and that exit is the wake. If
the watchdog cannot start (its command is refused, or it exits at once), the
orchestrator stops that turn right away and ends the run `blocked`: a turn with
no time bound is never left running. The two wakes:

- the turn's **wall-clock deadline** passes — `TIMEOUT`;
- the subagent's transcript **stops growing** for the stall threshold — `STALL`.

The normal path is not one of them. A turn that finishes sends its own completion
notification, and the orchestrator stops the watchdog task at that point. The
watchdog deliberately does *not* exit when the deliverable appears: a subagent
writes its file and only then composes its reply, so leaving there would wake the
orchestrator ahead of the turn's own notification, and would leave that
reply-writing tail with no time limit at all — the undetected wait the watchdog
exists to prevent. It still watches the file, and both `TIMEOUT` and `STALL` say
whether the turn had written it. That is what makes a hung tail legible: the
deliverable is there and the transcript went quiet.

"Written during this turn" is load-bearing in that message: a file already sitting
at the watched path would otherwise look like this turn's output (a re-run writes
to its own path, `<turn key>-<mode>-attempt<n>.md`, but the check does not rely on
that). The watchdog therefore counts only a deliverable newer than
its own start; an older one is noted once in its trace as stale and never
reported as this turn's output.

The watchdog only reports; it never stops anything itself. On `TIMEOUT` or
`STALL` the orchestrator first checks whether a well-formed reply is already in
hand (it then classifies the turn by that reply). Otherwise it stops the turn,
watches the stop for at most 60 seconds, and classifies the turn `aborted`; the
turn runs again only if the restart gate allows it (see `aborted` above) —
exactly as for an unreadable reply. No failure is diagnosed, because a turn that
never reported gives nothing to diagnose.

Defaults are at the top of the script — edit them there:

| Setting | Default |
|---|---|
| Stall threshold | 600s |
| Deadline, `survey` | 7200s |
| Deadline, `plan` | 4800s |
| Deadline, `execute` | 4800s |
| Deadline, `review-dev` | 3600s |
| Deadline, every other mode | 1800s |
| Poll interval | 15s |

They are deliberately generous. A long tool call looks exactly like a hang from
outside — the transcript is idle either way — so the stall threshold is set well
above the longest tool call a turn is expected to make. That detects a real hang
late rather than cutting a working turn short. If a run keeps aborting turns that
were still making progress, the budget is too tight; the run index records which
check fired, so you can see that and raise it. These are a first attempt's
budgets: a re-run runs under the seconds on its judge's `ALLOW` line, and after a
`TIMEOUT` that figure is longer than the deadline the attempt hit, backed by a
line of the attempt's own transcript.

The watchdog bounds time, not correctness. It can tell you a turn ran too long;
it cannot tell you a turn did the wrong thing.

## Workflow specs

A workflow spec supplies task-type guidance: a recommended sequence with stable
`id`, `mode`, `effort`, and `task` columns, plus the failure-policy caps. An
effort cell is either `(infer)` or one of `basic`, `pro`, and `ultra`; it never
names a model or establishes a general effort default for a mode.

A pin applies only when a numbered todolist step explicitly carries
`(workflow-step: ID)`. A bound `(infer)` row still uses final-instruction
classification. Without that metadata, every workflow row is guidance or a
warning only, and the turn uses inferred effort unless its own model or effort
metadata wins. Explicit model and effort always outrank a binding.

The skill ships two specs. Activate one with `--workflow=<name>` or by naming it
in the todolist. To add a spec for another task type, see
`WORKFLOW_SPEC_AUTHORING.md`.

- **`dev`** (`workflows/dev.md`), for development / implementation work
  (investigate → design → review → build → name the class → test → review →
  sync docs).
- **`research`** (`workflows/research.md`), for evidence-led research on a
  bounded question (method → gather → validate → reason → draft → challenge,
  with an optional second round for gaps the first one found). It also carries
  turn-facing evidence and scope policies, which reach a turn only when the
  todolist passes the spec file to that turn as an input.

## Run directory and artifacts

Each invocation creates one run directory at
`<ws>/tmp/runs/<YYYY-MM-DD>_<slug>/`, where `<ws>` is the directory the session
was started in (even if that is a worktree). A name already in use is never
reused: a suffix is added. The run never deletes the directory. It holds:

- `NN-<mode>.md` — one deliverable per turn, in order. Any turn inserted for
  turn `NN` — by either loop — takes the next free suffix letter, so a turn that
  goes through both reads as `05a-decision.md` → `05b-execute.md` →
  `05c-debug.md` → `05d-execute.md`. The mode is in the filename, so which loop
  produced which artifact is never ambiguous. A re-run after an abort writes
  `<turn key>-<mode>-attempt<n>.md` (`05-execute-attempt2.md`); the earlier
  attempt's files stay.
- One set of files per attempt, named `<turn key>-<attempt>-…` (`05-1-deny.txt`,
  `05-1-stop.json`, `05-1-evidence.json`, `05-1-judge-prompt.md`,
  `05-1-judge.md`; on Pi's CLI route also `05-1-pids.json`, `raw/` and
  `sessions/`). Pi CLI messages are saved as UTF-8 with the file-writing
  tool before the short shell call, at `<turn key>-<attempt>-prompt.md`.
  Each attempt keeps its own file. The judge's short launch message uses
  `<turn key>-<attempt>-judge-launch-prompt.md`, distinct from the
  `…-judge-prompt.md` instructions produced by the judge gate. These files
  remain with the run; they are not created by a long shell heredoc.
- `index.md` — the canonical run index. It contains the human-readable plan and
  exactly one machine-readable JSONL block; nested or prose `index.md` files are
  not run indexes. Before delegation, that block starts with the contract and
  initial plan records:

  ```json
  {"record":"contract","version":"mo-delegation-v2"}
  {"record":"plan","id":"r0","replaces":null,"after_turn":null,"digest":"<sha256>","approval_ref":"ap1","contingency_policy_ref":"cp1"}
  ```

  An initial turn always uses literal `"kind":"planned"`; its mode is recorded
  separately and is never a kind value. It then writes a planned turn definition —
  the selection records where the model request came from, its wording and scope,
  and what it resolved to:

  ```json
  {"record":"turn","key":"03","plan":"r0","kind":"planned","step":2,"parent":null,"inherits":null,"inputs":[],"mode":"plan","effort":"ultra","effort_source":"inferred-effort","budget_refs":{"recovery_owner":"03","decision_owner":"03","restart_owner":"03"},"rebinds":null,"amendment_ref":null,"selection":{"source":"skill","request":"(default profile)","scope_ref":"input doc step 2","resolution_ref":"res-03","resolved_model":"opus","granularity":"harness-ref","thinking":{"state":"undisclosed"}},"dispatch_plan":{"route":"native-subagent","args":{"model":"opus"}}}
  ```

  Only after a delegation call finishes or aborts does it append actual call
  evidence — the delegation record carries what the call actually carried:

  ```json
  {"record":"delegation","id":"d1","owner":"03","owner_kind":"turn","plan":"r0","approval_ref":"ap1","resolution_ref":"res-03","policy_ref":"perm1","route":"native-subagent","actual_selection_args":{"model":"opus"},"call_ref":"raw/03-1.jsonl","reported_model":"opus","reported_thinking":null}
  {"record":"attempt","turn":"03","attempt":1,"delegation_id":"d1","reported_status":"ok","effective_status":"ok","file":"03-plan.md"}
  ```

  `dispatch_plan` / `actual_selection_args` carry exactly one of model / effort /
  a fixed resolution reference; the actual one is what the call carried, never
  back-filled from the plan. The resolved model is never a tier. A run that stops
  before a call (denied permission, a changed resolution) records no delegation
  or attempt; a call that never started records its delegation with null reports
  and the attempt's effective status carries the classification (`blocked`,
  `stale-plan`, `aborted`). A split step may have several planned turn
  keys with the same numbered `step`, and an aborted retry adds the next attempt
  to the same turn. The index also records statuses, decision insertions,
  amendments, human-decision waits, and the check that caused an aborted retry;
  resolution, permission and approval details live in the run directory's
  append-only `approvals.jsonl` / `resolutions.jsonl` / `policy.jsonl`, referenced
  by the `*_ref` fields. A dynamic insertion rides the plan's approved
  contingency policy and carries a `derived` approval bound to it; its
  delegation references that derived approval.
  `scripts/delegation_plan.js validate` / `dispatch-check` / `audit` check the
  ledger's integrity — data consistency, not evidence authenticity; run them
  with the run's three evidence files, since a rule whose evidence is missing
  is a violation, not a skip.
  It is an inspection index, not a resumable scheduler. Previous
  `adaptive-effort-v1` runs stay read-only.

These are runtime artifacts — they are not committed.

## What it does not do

- No rollback / checkpoint / worktree around `execute` turns — working-tree safety
  is your git hygiene concern. Commit or stash WIP before an autonomous run. The
  skill does not set a worktree up; one a run creates anyway is the run's own: it
  is kept, and its deletion is reported as pending, unless the run is complete (or
  you discard it) and a separate removal rule (`run_owned_worktree_removal`) is in
  force.
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
- The bundled specs: `skills/mode-orchestrator/workflows/dev.md` and
  `skills/mode-orchestrator/workflows/research.md`.
