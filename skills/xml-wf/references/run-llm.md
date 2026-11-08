# LLM orchestration mode (run-llm): execute the XML as a control plane

Alternative to `--run-cc` (wfrun batch execution). Use it for interactive
sessions where the user wants to **watch each step land, grant permission,
and step in between steps**. When deterministic guarantees matter, use
`--run-cc`. (Intervening *between* steps is what this mode gives you; a step
that needs a ruling mid-task has to ask for one, which is the `DECISION:`
channel below — that works in both modes.)

This mode runs on Claude Code and on Pi, and every delegation it makes goes
through the host's native subagent facility: the Agent tool on Claude Code, the
`subagent` tool on Pi. It never starts a `claude` or `pi` process to run a step,
a debug diagnosis, a `<replan>` builder or a decider. `--run-cc` and `--run-pi`
are the modes that do, and this mode never moves to either by itself. The one
CLI it still starts is `wfrun ask`, under "Evaluating control structures".
Every platform other than Claude Code takes the Pi path, so one without Pi's
`delegation_contract` tool stops at the first row of the gate in Preparation.

## Your role (read first — it conditions every decision below)

You are the **control plane**, not a processor of task content. The design
principle of this mode is: **no task content ever enters your context** — LLMs
have a goal-completion bias (seeing the goal invites skipping procedure and
pre-empting results), and task content is precisely its fuel. The only things
allowed through your context are **the control skeleton, step ids, file paths,
ok/error, and true/false**. All content flows through wfrun and files:

- Task instructions are assembled **into files** by `wfrun prompt` (you never read them)
- Results are written **into files** by subagents (you never read them)
- Variables are written **into vars.json** by `wfrun record` (you never read or write it)
- Conditions are judged by `wfrun eval` / `wfrun ask --quiet` (you never compute them)

## Preparation (unconditionally, in this order)

1. `$WFRUN validate <xml> --backend llm` — on errors, stop and report (do not
   fix). `--backend llm` checks the XML itself and starts no CLI: it does not
   match model names against any catalog. On Pi, step 4 checks them against the
   host
2. `$WFRUN plan <xml>` — this output (control skeleton: ids, agents, branches,
   retry/on-error) is the only knowledge of the workflow you are allowed.
   **Do not read the XML itself**
3. `$WFRUN models <xml>` — the model requests this workflow will make, as
   control facts. On error, stop and report. Example output:

   ```
   harness=pi
   tier=pro model=sonnet sites=s1,s2
   model=gpt-x sites=s3
   tier=ultra model=opus sites=s2:decider
   ```

   `harness=` is `cc` when `CLAUDE_CODE_SESSION_ID` is set and not empty, and
   `pi` otherwise. Each further line is one request. `model=` is the name that
   `prompt` and `decide` print on their lines (the same resolution). `tier=` is
   present only when the name stands for `basic`, `pro` or `ultra`. `sites=`
   lists the step and `<replan>` ids that make the request, and `<id>:decider`
   for an llm decider. A step with no model has no line
4. ⟦NATIVE-GATE⟧ **The native subagent gate.** With `harness=cc` there is no
   gate: go on to step 5. With `harness=pi` every delegation of this run is a
   `subagent` call, so confirm before anything is created that the host offers
   it and can resolve every model of step 3. This step creates no file.

   Every stop in this step replies with one of its quotes: fill in the
   `<...>` values and copy the rest verbatim, however short a report you were
   asked for.

   a. Call `delegation_contract` with `{"operation":"observe"}`. Quote the
      `routes.native.state` it returns in your next output, then take the row
      that fits:

      | observe result | do |
      |---|---|
      | there is no `delegation_contract` tool | reply with the first quote below and STOP |
      | `routes.native.state` is `unavailable` | reply with the second quote below, its `reason` filled in, and STOP |
      | `routes.native.state` is `available` | keep the result's `settings` and go on to b |

      > run-llm cannot run here: this session has no native subagent route (the
      > `delegation_contract` tool is missing, so the subagent extension is not
      > installed or predates it). Set it up as described in "Pi setup" in
      > `docs/skills/xml-wf/README.md`, or run this workflow with `--run-pi` or
      > `--run-cc`.

      > run-llm cannot run here: the native subagent route is unavailable
      > ("<its reason>"). Set it up as described in "Pi setup" in
      > `docs/skills/xml-wf/README.md`, or run this workflow with `--run-pi` or
      > `--run-cc`.

   b. For each request line of step 3, call `delegation_contract` with
      `{"operation":"resolve","agent":"general-purpose","request":<R>,"requiredEffort":<E>}`.
      `<R>` comes from the line and from the `settings` of a:

      | the line has | `settings[<tier>].state` | `<R>` |
      |---|---|---|
      | `tier=` | `configured` | `{"effort":"<tier>"}` |
      | `tier=` | `absent` or `unset` | `{"model":"<the line's model=>"}` |
      | `tier=` | `invalid` or `unobservable` | no call: the line fails (c) |
      | no `tier=` | any | `{"model":"<the line's model=>"}` |

      `<E>` is the line's `tier=`, or `pro` for a line without one. It is only
      recorded: it never selects a model. A `tier=` is wfrun's fact: never
      derive a tier from a name, or a name from a tier
   c. Every line's call returned `resolved`: keep each line's `resolutionRef`
      for the whole run (it is bound to this session and its thinking level).
      Otherwise, once b has gone through every line, reply with this line for
      each line that failed, and STOP. Never replace the request with another
      one:

      > run-llm cannot run here: `<the models line>` is <state> ("<reason>"). See `<field>` in `child-session/config.json` and "Pi setup" in `docs/skills/xml-wf/README.md`, or run this workflow with `--run-pi` or `--run-cc`.

      `<state>` and `<reason>` are the result's; for a line with no call,
      `<state>` is `settings[<tier>].state` and `<reason>` is that entry's
      reason if it has one, otherwise `none`. `<field>` is `explicitModels` for
      a `model` request, `profiles` otherwise
5. **Fix `<ws>` before anything changes directory.** `<ws>` is the absolute
   path of the directory you were started in, even when that is a worktree —
   never one created later for this run. Every `<ws>` below means that path,
   wherever you work afterwards
6. Create the run dir `<ws>/tmp/runs/<name>_<ts>-llm/`. Below, `<run>` is its
   absolute path, and every run file is written under it. Keep the `-llm`
   suffix: the `vars.json` hook under "Enforcement boundaries" matches it. If
   that directory already exists, do not reuse it: take a fresh `<ts>`. Run
   `git -C <ws> check-ignore -q <run>`; if it is not ignored, say so in your
   first report and leave `.gitignore` alone. Write `<run>/vars.json` with the
   resolved `<param>` values (ask the user for missing required ones) — the
   single moment you write values
7. Create an empty `<run>/steps.log`. Every `--log` below names exactly this
   file: `prompt` counts the workflow's `max` from it

**From here on, every stop ends with one line.** Every "stop and report" and
every STOP below ends your report with this line, however short a report you
were asked for: fill in the two values and copy the rest verbatim.

> <id>: stopped (<cause>). This run dir cannot be resumed; the options are `--run-pi`, `--run-cc`, or a new run.

`<id>` is the step or `<replan>` you were handling. `<cause>` is what you
stopped on, verbatim: the first line of a wfrun command's output, or a
`resolve` result's `state`; `none` when there was no output. An interrupted
delegation on Pi — a step's, a diagnosis's or a builder's — ends with its own
line instead ("Delegating on Pi"), and the human
presentation of "On decision" is no stop: it waits for the answer.

Every step runs through the host's native subagent facility — on Claude Code the
Agent tool, on Pi the `subagent` call of "Delegating on Pi" — by the protocol
below: the steps run on the agent you are orchestrating from, whatever else is
installed. The model on the dispatch line comes from `model_map.json`'s `llm`
table. (`wfrun dispatch` / `wait`, which run a step through the claude CLI
instead, are not part of this mode.)

## Run dir and worktrees

A git worktree is this run's only if the run created it for its own work, and
that is recorded, not remembered:

- **Record it when you create it** in `<run>/worktrees.json`, a JSON array with
  one object per worktree: `path`, `branch`, `base`, `created` (the actual
  ISO 8601 time), `purpose`, and `disposition` (`delete-after-final`, or
  `retain` with who said so). A worktree not listed there is not this run's.
  It is reused only within this run.
- **When it may be deleted:** only after the run's final step is recorded, or
  when the user explicitly discards the run (a deadline or a missing reply is
  neither); and only by the separate rule `run_owned_worktree_removal`. No
  other worktree-removal rule, and no test or E2E cleanup procedure, is that
  rule.
- **Nothing else is deleted:** not `<ws>`, not a worktree this run did not
  create, not the run dir.
- **Keep the worktree** and report its deletion as pending, with every reason
  that applies, when: that rule is not in your context (do not search for it);
  `<run>` is not confirmed to sit outside the worktree; the worktree has
  uncommitted tracked changes; or its ownership is not confirmed.
- A run that stops before its final step has done no cleanup: its report says
  so.

## Step execution protocol (every step, these 4 moves in order)

**Dispatch immediately after move 1.** `wfrun prompt --result` starts one new
*attempt* of the step. The attempt gets its own result file, its own copy of
the prompt, and a completion sentinel carrying its own key (cycle, sequence
and a random token), so a late write by an earlier attempt can never pass for
this one's; `<run>/steps/<id>_handle.json` now points at it, and no earlier
attempt's result is deleted. `wfrun poll`'s deadline is measured from that
moment, not from whenever you happen to get around to move 2. Assemble, then
delegate — don't batch several steps' move 1 before delegating any of them.

**`prompt --result` is also the gate.** Before it writes anything it decides
whether a new attempt may start, and refuses (exit 1) when it may not: the
step's latest attempt has no recorded verdict yet; that attempt was aborted
and nothing has shown it safe to run again (see "On abort"); a decision of the
step has no ruling; the debug diagnosis the step waits for is not recorded;
run-cc's retry/debug rules would not re-run it (a refused tool call, spent
retries, `--fix` without a recorded debug RETRY, a second fixed re-run); or
the cycle's attempt cap or the workflow's `max` is reached. The refusal ends
in the one thing to do — do exactly that. The gate finds `<run>/steps.log` and
`<run>/decisions/` from where `--result` points, so write the paths exactly as
below: `--result <run>/steps/<id>_result.md` names the step, and every `--log`
is `<run>/steps.log`.

```bash
# 1. Assemble (output = path + dispatch facts only; never look inside)
#    The dispatch line shows resolved values: role=<name>|inline, mode=<name>,
#    model=..., tier=..., effort=..., tools=... — model is already
#    runner-resolved through model_map.json's `llm` table ("model=X (mapped from
#    Y)" when a mapping applied). A legacy name (haiku/sonnet/opus) on a step's
#    model= is first read as its tier (haiku as basic, sonnet as pro, opus as
#    ultra; model-legacy-name warns) and then looked up, so it prints a
#    resolved model= and a tier=. tier= is present only when the name stands
#    for basic, pro or ultra after that reading. On Claude Code pass model=
#    verbatim; on Pi "Delegating on Pi" says what the line's tier= and model=
#    become. Either way never translate a name or a tier yourself.
#    Then the attempt's own facts: attempt=cNNaNN, result=<this attempt's
#    result file>, timeout=<seconds>, description=<d>.
#    The prompt file already contains the full role and
#    mode text (joined into one file: neither delegation tool has a
#    system-prompt input, unlike run-cc's --append-system-prompt), so any
#    generic subagent will do. effort= is applied on neither harness: the
#    Agent tool has no effort input, and the `effort` of the Pi `subagent` call
#    is the tier profile of tier=, not this attribute
$WFRUN prompt <xml> <id> --vars <run>/vars.json \
    --out <run>/steps/<id>_prompt.md --result <run>/steps/<id>_result.md
#   [--fix "..."]          only as "On error" says
#   [--new-cycle]          only as "Cycles" below says
#   [--redispatch "..."]   only as "On abort" says

# 2. Delegate. One of these two fixed messages is ALL you hand to the
#    subagent; the dispatch line picks which (tool names are control facts, not
#    task content).
#    The line shows tools=...:
"Read <run>/steps/<id>_prompt.md and execute its instructions. Reading that
 file and writing the result file it names are always allowed; for everything
 else, use only these tools: <tools from the dispatch line>. The response
 protocol is inside the file. Reply to me with a single line starting with
 OK <id> or ERROR: <gist>."
#    The line shows no tools= (every <replan> builder, any step without one):
"Read <run>/steps/<id>_prompt.md and execute its instructions. The response
 protocol is inside the file. Reply to me with a single line starting with
 OK <id> or ERROR: <gist>."
#    On Claude Code, run the Agent call in the background, with the dispatch
#    line's model= when it shows one, and give it the dispatch line's
#    description= value verbatim as its description. It is
#    unique to this attempt, and wfrun finds the subagent's transcript by it
#    (record, stop, judge, the restart gate); any other description leaves
#    them unable to. In the same message, start the attempt's deadline watch
#    as a background command:
$WFRUN poll <run>/steps/<id>_handle.json --wait
#    It ends done(0) once the attempt's result is complete, or
#    deadline-exceeded(11) at the attempt's deadline, and writes nothing.
#    The subagent's completion notice is the normal wake: stop the watch
#    there. If the watch ends first: done -> move 3 (pass --reply only if one
#    has arrived); deadline-exceeded -> "On abort". A watch verdict that
#    arrives after the attempt is recorded is stale: ignore it. On Pi make the
#    `subagent` call of "Delegating on Pi" instead: there is no watch, the call
#    blocks, and its `timeout` is the deadline

# 3. Record (output = ok/error/aborted only; never look at the result body).
#    Pass back exactly the one reply line the subagent gave you — it is a
#    control fact (liveness signal), not task content. --result names the
#    step, not the attempt: record reads the attempt's own file through the
#    handle.
$WFRUN record <xml> <id> --result <run>/steps/<id>_result.md \
    --vars <run>/vars.json --log <run>/steps.log \
    --reply "<the subagent's one reply line, verbatim>"
#   ok(0) / error(1) / aborted(3) as before, plus:
#   decision(4)          -> the step asked for a ruling; see "On decision"
#   error: denied (details: <transcript>) (1)
#                        -> a tool call of the step, or of an agent it
#                           started, was refused. Whatever the result file
#                           says, this is never retried -- see "On error"
#   aborted: stopped before its result completed (3)
#                        -> the attempt has a recorded stop and its result never
#                           completed; no reply is read. A refused tool call, if
#                           one is found, is reported on a second line,
#                           "deny-check: DENIED <n> (details: <transcript>)" --
#                           see "On abort", 3
#   a second line "deny-check: NO-TRANSCRIPT: <reason>" after any verdict
#                        -> the refusal check could not run. The verdict
#                           stands but is unverified; say so in your report
#   error: ... (2)       -> nothing adopted: the attempt already has a
#                           verdict (go on from that one), or its result
#                           completed only after it was stopped or recorded
#                           aborted, which is never adopted (stop and report)

# No reply, a reply that is not a line starting with "OK <id>" / "ERROR:"
# (interrupted mid-turn, connection drop, garbled output), or a call that
# returned at its timeout: do NOT guess or wait indefinitely. Poll once —
$WFRUN poll <run>/steps/<id>_handle.json
#   done(0)               -> the result file completed after all; record it
#                            now (pass --reply if you did receive something,
#                            even if it looked malformed)
#   running(10)           -> the call ended before its deadline without a
#                            result: see "On abort"
#   deadline-exceeded(11) -> see "On abort". wfrun cannot kill the subagent:
#                            this is a verdict, not a termination

# 4. Report one line to the user ("<id>: ok/error/aborted", progress =
#    steps.log lines / max), move on. An aborted step on Pi does not move on:
#    its line is the one in "An interrupted delegation"
```

**Cycles (`<while>` / `<each>` re-visits).** The retry budget is per
*cycle* — one visit to the step node — exactly like run-cc, where every visit
gets a fresh retry count and `max` is the run-wide backstop. Each attempt's
files carry its cycle and sequence (`<id>_cNN_aNN_*`), so an iteration never
overwrites the previous one's audit trail. You normally do nothing: after a
cycle ends in `ok`, the next `prompt` of that same step starts cycle N+1
automatically (nobody retries a success). Pass **`--new-cycle`** only when the
previous iteration ended in a *failure the workflow tolerated*
(`on-error="ignore"`) and you are moving on to the next iteration anyway —
there the ledger cannot tell "accepted and moved on" from "about to retry", so
you must say so. `prompt`'s refusal names the flag when this is the likely
cause. It never follows an aborted attempt: `prompt` refuses it there, and the
step goes on only as "On abort" says.

### Delegating on Pi

On Pi every delegation of this run is one `subagent` call. The gate in
Preparation has already shown that the route exists and that every model
request resolves, so nothing here chooses a route or a model again: a refusal
now is a stop.

**The call.** Single mode:

- `agent: "general-purpose"` and `nested: false`. Leave `agentScope` out, as the
  `resolve` calls did.
- `task`: the delegation's fixed message.
- top-level `timeout` (seconds): from the table below. Every delegation has one.
- With a model: `expectedResolution` is the `resolutionRef` kept for the
  request. The request is the line of `$WFRUN models` whose `sites=` holds this
  delegation's id (`<id>:decider` for a decider; for a step of a `<replan>`
  child, the line of the child's own `models`). Pass neither `model` nor
  `effort`.
- Without a model (the line shows no `model=`): pass no `expectedResolution`,
  `model` or `effort`. The child follows the parent's or the agent's default.

The call carries no tool restriction and no working directory: the dispatch
line's `tools=` is only the sentence in the message ("Enforcement boundaries").

**The reply.** The returned content begins with one or more `[subagent]` lines
(`evidence=`, then `outcome=`, and others). Split off every leading
`[subagent]` line: they are the tool's control lines, not the child's reply.
Give the `run=` value in the step's report line (move 4). Only the child's one
reply line goes to `record --reply`.

"Normal" below is `outcome=completed` with no `unconfirmed_descendants`. Every
call carries a `timeout`, so a call that ran has an `outcome=` line. A call
without one was refused before it ran (a schema rejection, `stale:` or
`blocked:`) and takes the "otherwise" column.

| delegation | `timeout` | model | normal | otherwise |
|---|---|---|---|---|
| step | the dispatch line's `timeout=` | the dispatch line's `tier=` / `model=` | move 3: `record` decides from the result file | `poll` once. `done` → `record` (with `--reply` if the child's reply line arrived). Anything else → "An interrupted delegation" below |
| debug diagnosis | 600 | none | `record --debug-reply` | stop and report; never delegate it again |
| `<replan>` builder | the dispatch line's `timeout=` | the dispatch line's `tier=` / `model=` | go on to `validate` | stop and report; do not `validate` |
| decider | the `timeout=` of the `decide` line | the `decide` line's `tier=` / `model=` | `record --answer` | run the same `record --answer`; on `error(2)` take the human path ("On decision") |

**An interrupted delegation.** A step's attempt that is neither normal nor
`done` after the `poll` is recorded and the run stops:

1. Record it: move 3's command without `--reply`. It ends `aborted(3)`.
2. Report. For a refusal, give the refusal's own reason (no new
   classification: `stale:` and `blocked:` are reported, not repaired; never
   repeat the call without `expectedResolution`, and never pass a model of your
   own). When the outcome is `stop_unconfirmed`, say that the child may still be
   running and that wfrun cannot stop it.
3. End the report with this line, and STOP. Fill in the two values (`none` for
   one the call did not return) and copy the rest verbatim, however short a
   report you were asked for. It replaces move 4's line:

   > <id>: aborted run=<the run= value> outcome=<the outcome= value>. This run dir cannot be resumed; the options are `--run-pi`, `--run-cc`, or a new run.

A diagnosis or a builder stops as its row says, with the report of step 2
above, and that report ends with the same line, `<id>` being the step
diagnosed or the `<replan>`. A decider goes on as its row says.

**A reply cut off by the timeout is survivable, by design.** A missing reply is exactly what
`poll` is for: it returned `done`, and `record` without `--reply` then produced
`ok` from the completed result file. Do not treat a step as aborted just
because you did not see its reply — poll first.

## No `<parallel>` support

run-llm executes sequentially only. `<parallel>` children must be run one
after another, each through the full 4-move protocol, same as a plain
sequence. Do not delegate more than one step's subagent before that step's
`record` has returned.

## Evaluating control structures

- `test=` : `$WFRUN eval "<expr>" --vars <run>/vars.json` — branch only on the
  literal `true`/`false` it prints
- `ask=` : `$WFRUN ask "<question>" --vars <run>/vars.json --quiet --log <run>/steps.log` —
  same (the reason goes straight to the log file; you do not see it). The
  dispatch string above stays fixed — do not add `--backend`: `ask` detects
  the running harness on its own (`CLAUDE_CODE_SESSION_ID` set → the claude
  CLI, unset → the pi CLI) and logs which one ran under `"backend"` in each
  steps.log entry. When it ran against the pi CLI, `cost_usd` in that entry
  is always `0.0` — a `0.0` there means "not measured", not "measured zero";
  only claude-CLI entries carry a real cost. `ask=` is the one exception to
  this mode's native-only delegation: `wfrun ask` starts that CLI itself, is
  not a delegation, and needs neither the Preparation gate's route nor a
  `subagent` call, so it works where delegation does not
- `while`/`each` : repeat the full 4-move protocol every iteration. Stop and
  report when the workflow's `max` is reached — count only steps.log's
  **step-execution** entries (the ones carrying a `"step"` field). `ask`
  judgments share this log file but are condition evaluations, not step
  executions, and do not consume the cap. (`prompt` enforces this same count
  in code, so it will refuse rather than let you overrun.)

## `<replan>` nodes (dynamic continuation, one level deep)

1. `$WFRUN prompt <xml> <replan-id> --vars <run>/vars.json --out <run>/steps/<id>_prompt.md
   --result <run>/replans/<id>.xml` — assembles the **builder** prompt (same
   firewall: you never see it). Each builder attempt writes its own file,
   printed as `result=` (`<run>/replans/<id>_cNN_aNN.xml`); earlier ones stay.
   No sentinel or handle.json here — a replan's own XML well-formedness,
   checked next, already proves it is complete
2. Delegate with the fixed message for a line with no `tools=`: a builder's
   line never shows one (the builder role is inside the file),
   by the delegation tool of your harness ("Delegating on Pi", the `<replan>`
   builder row)
3. Validate the generated continuation **programmatically**:
   `$WFRUN validate <the result= file> --as-child --backend llm --defined-vars <run>/vars.json`
   - errors → re-delegate from 1 with `--fix "<the validator error lines>"`;
     `prompt` admits that at most `retry` times, then refuses and names the
     on-error to follow. It refuses any re-delegation while the last
     attempt's file is missing or is not well-formed XML: whether that builder
     stopped is unknown then — stop and report
4. `$WFRUN plan <the result= file>` — the child control skeleton. On Pi, then
   run `$WFRUN models <the result= file>` and `resolve` each of its request
   lines as in Preparation step 4b; if any is not `resolved`, create no
   attempt of the child's steps: report it and STOP. Execute the skeleton with
   this same protocol, then continue after the replan node. `prompt` holds the
   child's steps to the child's own `max`, counting only those steps; they also
   count toward the parent's `max`, which `prompt` checks at the parent's next
   step. Generated continuations must never contain another
   `<replan>` (the validator enforces this)

## On error (authority you do NOT have)

When record returns `error`, run move 1 again for the same step, without
`--fix`. `prompt` applies run-cc's retry/debug rules in code: it starts the
next attempt while `retry` remains, and otherwise refuses with what `on-error`
(shown by plan) leaves — `fail` = stop and report / `ignore` = move on (a
later iteration of the same step then takes `--new-cycle`) / `debug` =
delegate a diagnosis subagent, once (on Pi, the "debug diagnosis" row of
"Delegating on Pi"), with: "You are the debug role defined in
.claude/agents/debug.md — read that file and adopt it. Then read
<run>/steps/<id>_prompt.md and <result file>, diagnose, answer RETRY or FAIL,
and if RETRY write fix instructions to <run>/steps/<id>_fix.md", where
<result file> is the failed attempt's `result=`. Then record its one-line
answer:

```bash
$WFRUN record <xml> <id> --result <run>/steps/<id>_result.md \
    --vars <run>/vars.json --debug-reply "<the diagnosis's one reply line, verbatim>"
#   debug: RETRY ... (0) -> redo from move 1 with
#                           --fix "$(cat <run>/steps/<id>_fix.md)", exactly once
#   debug: FAIL ... (1)  -> stop and report
#   error ... (2)        -> nothing recorded: the reply is neither RETRY nor
#                           FAIL, a RETRY wrote no fix file, or this attempt
#                           already has its diagnosis. Stop and report
```

A diagnosis that never replies leaves nothing to record: stop and report,
and never delegate it again — wfrun keeps no record of a diagnosis being
delegated, so only this rule stops a second one.
**`error: denied` is the exception.** A tool call was refused, so do not
retry the step and do not delegate a debug subagent. Follow `on-error` as if
the retries were spent, with `debug` read as `fail`: the same call would be
refused again, and a rerun could reach the refused effect another way.
`prompt` refuses that re-run in code too.
**You have no authority to read result files, debug, devise workarounds, or
fabricate substitute results.**
Stopping is not failure in this mode — every trace is on disk and the user can
decide; that is the normal terminal state.

## On decision (`record` exit 4) — the step hit a fork it may not settle

A `DECISION:` response is **not an error**. It never enters `retry`,
`on-error`, or the abort machinery: the step did not fail, it asked a
question it is not allowed to answer alone. Answering it is never *yours* —
you have no more authority to pick a branch here than the step did. Who may
answer instead is the workflow's `decider:`, which `plan` printed for you.

`record` already filed the request in `<run>/decisions/`, outside the step's
attempt files, and printed both paths.

1. **`decider: human` (the default) — present the two paths and stop.** Say
   which step asked, give the request path and the answer path. Do **not**
   read the request — same firewall as everywhere else in this mode; the user
   reads it, you carry paths
   - **`decider: llm` — delegate it, the same way you delegate a step.**
     First issue the delegation:
     `$WFRUN decide <xml> <id> --result <run>/steps/<id>_result.md` prints
     `<request path> (decider, model=..., [tier=...,] answer=<answer path>,
     timeout=<seconds>, description=...)`.
     Spawn one subagent with that model — on Claude Code, with that
     description; on Pi, as the "decider" row of "Delegating on Pi" says — and
     pass it **paths only**: the request path and the
     answer path. You still do not read the request. `decide` delegates a
     request once; when it refuses instead (exit 1 or 2: the decider is not
     llm, the llm decider has spent its rulings here, or the request was
     already delegated), fall back to the human presentation above
   - The delegation message says exactly this and nothing about the task:
     *rule on the decision request at `<request path>`; write the ruling to
     `<answer path>` with `option: <N|none>` as its first line and your
     reasons after it; do not settle a fork that is irreversible,
     outward-facing, or changes what the workflow is for — for those, and
     whenever you are unsure, write no file and make `escalate: <ground>` your
     final response. Your final response is exactly one line: `OK` when you
     wrote the ruling, or `escalate: <ground>` when you did not. `<ground>` is
     one of irreversible / outward-facing / changes-the-goal / uncertain, plus
     at most a short clause. Put nothing else in it — no figures, no file
     contents, no quotes from the request. Your reasons belong in the ruling
     file, which nobody but the next step reads*
   - **Why the one-line rule**: the reply channel is the one way task content
     can still reach you. Measured 2026-08-13: adjudicating subagents put the
     chosen option's reasoning, `orders.csv` contents and the figures
     `300 vs 225` into their final responses, and it landed in the
     orchestrator's context — the delegation passed paths only, but nothing
     constrained the reply. Like every other prompt-level rule here this is a
     likelihood, not an enforcement (see "Enforcement boundaries"); the
     firewall it guards is one-way by construction
   - **Cost of the rule, accepted**: in this mode an escalation reason is
     therefore a category, not an explanation. The person you hand the fork to
     reads the request file themselves. (The batch path keeps the full reason
     in the decision event's `adjudication_note`, since no orchestrator context
     is at risk there.)
   - If that final response starts with `escalate:`, or the verdict message
     told you an llm decider has already spent its rulings here, fall back to
     the human presentation above — and when the user answers, add
     `--decider human` to the command below so their ruling is not counted
     against the llm's budget
   - Otherwise run the command below unchanged. Do not check whether the file
     exists: `record` does that, and a missing or unusable ruling comes back
     as `error(2)`, which is your signal to fall back to the human path
2. When the ruling has been written (by the user, by the subagent, or by you
   under the user's dictation — then you write exactly that, nothing more):

```bash
$WFRUN record <xml> <id> --result <run>/steps/<id>_result.md --vars <run>/vars.json \
    --log <run>/steps.log --answer <run>/decisions/<id>_dNN_answer.md
#   [--decider human]  add this when a person answered a request that fell
#                back to them under `decider: llm`
#   ok(0)     -> settled AND the step's deliverable already stood; vars.json
#                updated from the request's own output. Nothing re-runs
#   rerun(5)  -> settled, and the step must run again: redo from move 1.
#                The ruling is injected into the new prompt automatically —
#                you pass nothing, and every earlier ruling for this step
#                rides along too, so a settled fork cannot re-open
#   error(2)  -> nothing was settled: no open request, no answer file at that
#                path, an invalid one (needs a leading `option: <N|none>`), or
#                a request already answered. State is unchanged. After a
#                delegated ruling this means the delegation failed: fall back
#                to the human path rather than delegating again
```

3. Report one line and continue

The answer file's first line must be `option: <number>` (from the request's
own numbered list) or `option: none` followed by free text saying what to do
instead. Nothing else about the file is parsed.

**A decision consumes the workflow's `max`** — the entry lands in steps.log
with a `"step"` field, like any other attempt, because it *was* a real
execution. `prompt` widens the cycle's attempt cap to match, so a ruling
never costs you a retry you needed.

**The llm decider's budget is counted for you.** Two rulings per step, after
which the verdict message says so and asks for a person; human rulings marked
`--decider human` never count. You keep no tally — the ledger in
`<run>/decisions/` is the count, and `record` and `decide` read it.

## On abort (a deadline passed, a call ended without a valid result, or `record` exit 3)

`aborted` is a different failure mode from `error` and is handled
separately, never folded into the `retry`/`on-error` machinery above: it
means the subagent went silent (interrupted, connection drop, killed
mid-turn, out of time) rather than finishing and reporting a failure.
Retrying it like an ordinary `error` would burn the step's `retry` budget on
an infrastructure event the step itself never got a chance to fail at, and
handing it to `debug` makes no sense either — there is no execution to
diagnose.

An attempt that may have started is run again only when four things are
shown, and `prompt` checks every one before it writes anything:

1. the attempt, and everything of it wfrun can watch, has ended and can no
   longer write — a *confirmed* stop;
2. its own result is still incomplete — a completion inside the stop's
   observation window is adopted instead;
3. a judge that read the evidence found that running the step again from the
   start cannot duplicate any effect the attempt already had;
4. the budget allows it: one restart after an abort per cycle, inside the
   cycle's attempt cap and the workflow's `max`.

A deadline, a timed-out call, TaskStop's own answer and a subagent's own
claim are none of these. When any of the four is missing, the step is not run
again: stop and report.

**On Pi the first of the four can never be shown.** Nothing observes the stop of
a `subagent` child: there is no confirmed stop, no judge and no restart. An
aborted attempt on Pi is recorded and the run stops; this run dir cannot be
resumed, and the report ends with the line of "An interrupted delegation".
If `prompt` is run anyway, its refusal names `wfrun stop` or `wfrun judge`: on
Pi neither applies; stop and report.
Steps 3, 5 and 6 below are Claude Code's. In order:

1. **Check for completion.** The watch's `deadline-exceeded`, or `poll` after
   a call that ended without a valid reply, is this check. `done` → record
   it; nothing below applies
2. **Request the stop, right after that check.** On Claude Code, TaskStop the
   step's Agent task — unless its completion notice already arrived, which
   leaves nothing to stop. On Pi the `subagent` call's `timeout` already
   requested the stop, cooperatively, and a call that returned before its
   deadline has ended
3. **Observe the stop (Claude Code only; on Pi go on with 4 and do not run
   `$WFRUN stop`).**

   ```bash
   $WFRUN stop <run>/steps/<id>_handle.json --facet cc --task-stop <stopped|completed|failed>
   #   --task-stop: what TaskStop reported -- stopped; completed (the
   #   completion notice had already arrived, so there was nothing to stop);
   #   failed (it errored or could not be called)
   #   done(0)         -> the result completed inside the window: record it
   #                      (adopted); nothing below applies
   #   confirmed(12)   -> go on with 4
   #   unconfirmed(13) -> record the attempt, then stop and report: it is
   #                      never run again
   #   error: ... (2)  -> nothing was observed (a wrong flag, a handle that is
   #                      not an attempt's, or an attempt already recorded with
   #                      an outcome other than aborted): stop and report
   ```

   It watches for at most 60 seconds and writes the attempt's stop record,
   once: asking again prints the first verdict. A reply that arrives after
   the stop request changes nothing by itself; what counts is the result file
   `stop` watches. It confirms only when TaskStop reported the
   task stopped or already ended, the transcripts of the subagent and of
   every agent it started are found, none of them grew during the window,
   and none of them holds a tool call without a result, one that started
   work in the background, or one refused or interrupted. A TaskStop that
   interrupts a running call can leave it recorded as refused: the stop then
   stays unconfirmed, and `record` still records the attempt `aborted`, with
   the refusal check's verdict on a `deny-check:` line and in `steps.log`: a
   stop is not a denial. It looks at neither processes outside what it
   watches nor anything done outside files; the stop record names what it did
   not observe. On Pi no facet exists for a `subagent` child: the call's
   `outcome=stopped` says a cooperative stop was confirmed and
   `outcome=stop_unconfirmed` says the child may still be running, but wfrun
   checks neither, so record the attempt (4), then stop and report
4. **Record** the attempt — move 3's command without `--reply`, whatever
   reply arrived: the attempt is settled by its file and, on Claude Code, its
   stop, so `aborted` (3) follows, and `record` reads no reply after a stop that
   ended without a completion. A refusal it prints instead ends in what to do
5. **Issue the judge** (Claude Code, a confirmed stop only; on Pi none is
   issued):

   ```bash
   $WFRUN judge <xml> <id> --result <run>/steps/<id>_result.md
   #   <judge prompt> (judge, model=..., tools=..., record=<record>,
   #                   handle=<judge handle>, timeout=<seconds>, description=<d>)
   #   error: ... (1 or 2) -> no judge is issued: stop and report
   ```

   It refuses unless the attempt is recorded `aborted` with a confirmed stop
   and a result still incomplete, and also when a tool call of the attempt
   was refused, its transcript cannot be found, a decision is open, the
   restart would be over budget, or this attempt already had its judge.
   Otherwise it lists the evidence with checksums and writes the judge's
   prompt. Delegate the judge like a step, with the printed model and tools
   and the fixed message "Read <judge prompt> and execute its instructions.
   Use only these tools: <tools from the judge line>. Reply to me with the
   single line it asks for." Give the Agent call the printed
   description, run it in the background, and in the same message start
   `$WFRUN poll <judge handle> --wait` as its deadline watch. A judge that
   has not replied by its deadline, or
   whose reply is not the one line `allow <record>` / `deny <record>`, is a
   refusal: TaskStop it if it still runs, then stop and report. A judge is
   never delegated twice
6. **Restart, or stop** (Claude Code). `deny <record>` → stop and report. `allow <record>` →
   move 1 again, adding `--redispatch "<the judge's reply, verbatim>"`.
   `prompt` admits it only when the record allows a restart from the start
   and nothing else, states its limits, cites at least one line range of a
   listed evidence file, leaves every listed file unchanged, was written
   within the judge's deadline and ends in its end marker, and — after a
   deadline — gives a longer `next-timeout`, backed by a cited transcript
   line from inside the attempt's run; the new attempt then gets that timeout
   (`timeout=` on its dispatch line). Any refusal: stop and report

A second abort in the same cycle stops: `judge` and `prompt` refuse it. A
result that completes after its attempt was stopped or recorded aborted is
never adopted: `record` says so, and `prompt` then refuses every further
attempt of the step — report it. This budget is independent of the step's
own `retry` attribute (aborted attempts do not consume it, and its exhaustion
does not affect the one restart after an abort).

## Enforcement boundaries (what is NOT deterministic here)

**What code decides here:** telling "the subagent finished writing" apart
from "the file exists but is partial/stale/never touched" no longer depends
on trusting the subagent's self-report. The completion marker is keyed to its
attempt and checked by code (`record`, `poll`, `stop`), not inferred from
prose; a missing result file or a missing marker is a *fact* about the
filesystem, not a judgment call. `record --reply` further cross-checks the
reply channel against the file channel (reply claims OK but the file is
missing/incomplete/mismatched -> flagged, never silently trusted either way).
Whether another attempt may start is decided by `prompt`, not by you: the
retry/debug rules, the budgets, an open decision, and after an abort the stop
record, the refusal check and the judge's record.

**What stays advisory:** the reply-channel liveness signal itself. Nothing
forces the subagent to reply with the exact `OK <id>` / `ERROR:` format, or
forces it to reply at all before going silent — that is exactly the
scenario the deadline watch and `poll`'s `deadline-exceeded` exist to catch
from the outside. wfrun cannot terminate a still-running subagent: it has no
kill authority in this mode. The stop is requested by you (TaskStop) or by
the `timeout` of the Pi `subagent` call, which is a cooperative stop. `stop`
observes only Claude Code transcripts; nothing observes a Pi child, so a
`stop_unconfirmed` child may keep running. And nothing here defends against you, the orchestrator, fabricating a
`--reply` value that does not match what the subagent actually said — pass it
verbatim or omit it. The same holds for the judge: `prompt` checks its
record's form, citations, checksums and timing, not who wrote it.

**The native gate is prompt-level.** wfrun cannot see the host's tools, so
nothing in code stops an orchestrator from skipping Preparation step 4 or
delegating another way. What holds on Pi is the host's: a call pinned with
`expectedResolution` is refused (`stale:` / `blocked:`) instead of running on
another model.

**`ask=` is not delegated.** `wfrun ask` starts the claude or pi CLI itself and
runs whether or not a native route exists ("Evaluating control structures").
It is the one place this mode starts a CLI, and its judgment lands only in the
log.

Three more of run-cc's structural guarantees degrade in this mode; know
them, do not paper over them:

- **`tools=` is advisory, on Claude Code and on Pi.** The Agent tool has no
  per-call tool restriction, and the Pi `subagent` call passes none, so the
  dispatch line's tools cannot be enforced on the subagent — the
  "use only these tools" sentence in the fixed message for a line with
  `tools=` is a likelihood lever
  only. That sentence exempts the two file operations this protocol needs of
  every step, reading its prompt file and writing its result file, so a list
  without `Read` or `Write` does not cover them. When enforcement is needed,
  use `--run-pi` or `--run-cc`. Step
  `expect-file`/`schema` checks (`wfrun record`) still verify
  outcomes deterministically. The judge's tool list is advisory too;
  `prompt` refuses its record when any listed evidence file changed.
- **The no-read firewall is prompt-level.** Nothing mechanically stops the
  orchestrator from Reading prompt/result files. A partial deterministic
  backstop: add this PreToolUse hook to the project's `.claude/settings.json`
  (restart the session to load it; `wfrun prompt` prints a note when the
  marker is absent). It denies Read/Grep/Glob on the run's `vars.json` — the
  one file no agent ever legitimately reads through tools (wfrun accesses it
  in-process; step and debug subagents never touch it):

  ```json
  {
    "hooks": {
      "PreToolUse": [{
        "matcher": "Read|Grep|Glob",
        "hooks": [{
          "type": "command",
          "command": "uv run python -c 'import json,sys; d=json.load(sys.stdin); p=\" \".join(str(v) for v in (d.get(\"tool_input\") or {}).values()); b=\"-llm/vars.json\" in p; b and sys.stderr.write(\"xml-wf-llm-guard: run-llm vars.json is off-limits to agents\"); sys.exit(2 if b else 0)'"
        }]
      }]
    }
  }
  ```

  Prompt/result files stay hook-free by design: hooks fire for subagent tool
  calls too, and the step subagent must Read its prompt file (the debug
  subagent its result file) — a broader deny would break them. And any hook
  can be bypassed via Bash file reads, so this closes the *accidental* read
  path, not the deliberate one. The primary defense remains ⟦STEP-GATE⟧ below.
  A step subagent that reads `vars.json` anyway is refused by this hook, so
  `record` reports that step `error: denied`.

  The hook is a Claude Code mechanism, so this mechanical backstop only
  exists there. Outside Claude Code, `wfrun prompt --result` prints a note
  that the firewall is prompt-level only rather than searching for the
  marker (there is no `.claude/settings.json` hook system to check) —
  ⟦STEP-GATE⟧ is what carries the guarantee in that case.
- **Refused tool calls are found only on Claude Code, and only through the
  description.**
  - `record`, `stop`, `judge` and `prompt --redispatch` locate the step
    subagent's transcript by the `description=` you gave the Agent call. They
    look only among the agents of your own session
    (`CLAUDE_CODE_SESSION_ID`), together with every agent that subagent
    started.
  - If you give it another description, they find nothing: `record` says
    NO-TRANSCRIPT, and a restart after an abort is refused.
  - A subagent that replies while agents it started are still running can
    leave their transcripts incomplete when `record` reads them.
  - Pi has no permission layer, so nothing is checked there.
  - `prompt` refuses to re-run an attempt recorded `error: denied`, and
    refuses a restart after an abort whose transcripts hold a refused call.

---

⟦STEP-GATE⟧ Immediately before issuing each delegation, verify four conditions —
(1) your previous output quotes the path line printed by `wfrun prompt` (for a
    judge, by `wfrun judge`; for an llm decider, by `wfrun decide`)
(2) the delegation message is the fixed text above for that line, containing
    no summary or paraphrase of task content
(3) you have not Read the prompt, result, or vars.json for this step
(4) on Pi: the call carries the `timeout` of its "Delegating on Pi" row, and,
    when the line shows `model=`, `expectedResolution` is the `resolutionRef`
    kept in Preparation 4c. None kept means the gate has not run: run it first.
If any is missing, do not delegate — perform the missing move first.
"Checking the prompt file's content", "summarizing results", and "batching
multiple steps" are not diligence or efficiency; they are protocol violations.
