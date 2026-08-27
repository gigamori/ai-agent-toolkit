# LLM orchestration mode (run-llm): execute the XML as a control plane

Alternative to `--run-cc` (wfrun batch execution). Use it for interactive
sessions where the user wants to **watch each step land, grant permission,
and step in between steps**. When deterministic guarantees matter, use
`--run-cc`. (Intervening *between* steps is what this mode gives you; a step
that needs a ruling mid-task has to ask for one, which is the `DECISION:`
channel below — that works in both modes.)

This protocol is platform-independent: any agent with wfrun (Python) and a
subagent facility (the Agent tool in Claude Code; the equivalent subtask
delegation elsewhere) can act as the orchestrator.

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

1. `$WFRUN validate <xml>` — on errors, stop and report (do not fix)
2. `$WFRUN plan <xml>` — this output (control skeleton: ids, agents, branches,
   retry/on-error) is the only knowledge of the workflow you are allowed.
   **Do not read the XML itself**
3. Create the run dir `runs/<name>_<ts>-llm/` and write `vars.json` with the
   resolved `<param>` values (ask the user for missing required ones) — the
   single moment you write values
4. Create an empty `steps.log`
5. **Choose the protocol layer** (reliability-spec.md §5.2): run
   `claude --version`. Success → **layer A** (`dispatch`/`wait`, next
   section — deterministic timeout/kill, no subagent delegation needed);
   failure or unavailable → **layer B** (subagent delegation +
   `record`/`poll`, the section after that). An explicit user instruction
   to use one layer overrides this probe. Layer choice is made once per
   run, not per step.

   **What the layer actually decides is who executes the steps** — layer A
   runs every step through the claude CLI, layer B runs it through your own
   subagent facility. The probe only asks whether the claude CLI is
   available, so on a non-Claude-Code harness that also has claude installed
   it selects A, and **the steps then run on claude rather than on the agent
   you are orchestrating from**. That is usually what you want (A is the
   layer with deterministic timeout, kill, and attempt caps), but when the
   point of the run is to execute on this harness, say so and select layer B
   explicitly. Layer B's model names come from `model_map.json`'s `llm`
   table — the names *your* execution facility accepts — while layer A's
   come from the `cc` table.

## Layer A: step execution protocol (`claude --version` succeeded)

No subagent delegation, no reply channel to relay — `wfrun` itself makes
the `claude -p` call inside a detached, self-timing-out wrapper process,
so a single Bash-tool call (which has its own ~600s ceiling) never has to
block for the step's full duration.

```bash
# 1. Dispatch (returns immediately; the wrapper runs in the background)
$WFRUN dispatch <xml> <id> --vars vars.json --run-dir runs/<name>_<ts>-llm \
    [--permission-mode acceptEdits] [--fix "<debug fix instruction>"] \
    [--new-cycle]
#    Prints the handle path plus the cycle and attempt it started:
#    runs/.../steps/<id>_c01_handle.json (dispatched, pid=..., cycle=1, seq=1)

# 2. Wait (blocks up to --max seconds watching for completion; call again
#    if it reports "running" — keep --max <= 550 so one call always fits a
#    single Bash tool invocation)
$WFRUN wait runs/.../steps/<id>_handle.json --max 500 \
    --vars vars.json --log steps.log
#   ok            (0)  -> vars.json updated; move on
#   error: <class> (1)  -> see "On error" below (same policy as layer B)
#   decision: ...  (4)  -> the step asked for a ruling; see "On decision"
#   running        (10) -> not done yet; call wait again
#   aborted: ...   (3)  -> the wrapper itself never finished (crashed/killed
#                          externally) -- see "On abort" below, same as layer B
#
# Calling wait again on an ALREADY-FINISHED handle is safe and expected:
# it replays the same verdict and exit code without re-appending to
# steps.log or re-running a debug diagnosis. Only a fresh `dispatch`
# starts a new attempt.

# 3. Report one line to the user ("<id>: ok/error/aborted", progress =
#    steps.log lines / max), move on
```

On-error/on-abort follow the **same policy** as layer B ("On error" /
"On abort" sections below): retry via `dispatch` again (no `--fix`) up to
`retry` times; on exhaustion, `on-error="debug"` is handled *for you* by
`wait` itself (it runs the debug diagnosis in-process and writes
`steps/<id>_fix.md` on RETRY — you do not delegate a debug subagent in this
layer) — then re-dispatch once with `--fix "$(cat steps/<id>_fix.md)"`.
On `aborted`, re-`dispatch` the same step exactly once; a second `aborted`
stops and reports. **The attempt cap is enforced by `dispatch` itself**
(reliability-spec.md §5.1, F4/P5): a `retry`+1+debug(1)+aborted-redispatch(1)
budget is read back from that cycle's `steps/<id>_cNN_attempts.json` before
any dispatch is allowed to proceed, so you cannot accidentally loop past it
even by mistake.

**Cycles (`<while>` / `<each>` re-visits).** That budget is per *cycle* —
one visit to the step node — exactly like run-cc, where every visit gets a
fresh retry count and `max` is the run-wide backstop. Each cycle also gets
its own `_cNN_` prompt/result/handle files, so an iteration never
overwrites the previous one's audit trail. You normally do nothing: after
a cycle ends in `ok`, the next `dispatch` of that same step starts cycle
N+1 automatically (nobody retries a success). Pass **`--new-cycle`** only
when the previous iteration ended in a *failure the workflow tolerated*
(`on-error="ignore"`) and you are moving on to the next iteration anyway —
there the ledger cannot tell "accepted and moved on" from "about to retry",
so you must say so. `dispatch`'s cap message names the flag when this is
the likely cause.

## Layer B: step execution protocol (`claude --version` unavailable, or user-selected; every step, these 4 moves in order)

**Dispatch immediately after move 1.** The prompt file's response protocol
now embeds a completion sentinel keyed to *this* attempt's dispatch time
(`wfrun prompt --result` also deletes any leftover result file from a
prior attempt and writes `steps/<id>_handle.json`); `wfrun poll`'s deadline
is measured from that dispatch moment, not from whenever you happen to get
around to move 2. Assemble, then delegate — don't batch several steps'
move 1 before delegating any of them.

```bash
# 1. Assemble (output = path + dispatch facts only; never look inside)
#    The dispatch line shows resolved values: role=<name>|inline, mode=<name>,
#    model=..., tools=... — model is already runner-resolved through
#    model_map.json ("model=X (mapped from Y)" when a mapping applied): pass
#    the resolved name verbatim, never translate it yourself. A step still
#    carrying a legacy name (haiku/sonnet/opus) is NOT rebound here -- it
#    prints unresolved, since only the model= dispatch path aliases legacy
#    names (model-legacy-name warns); migrate the step's model= instead.
#    The prompt file already contains the full role and
#    mode text (joined into one file: the Agent tool has no system-prompt
#    input, unlike run-cc's --append-system-prompt), so any generic subagent
#    will do; apply model/effort from the dispatch line when delegating
$WFRUN prompt <xml> <id> --vars vars.json \
    --out steps/<id>_prompt.md --result steps/<id>_result.md

# 2. Delegate (this fixed message is ALL you hand to the subagent; when the
#    dispatch line shows tools=..., include the second sentence verbatim with
#    that list — tool names are control facts, not task content)
"Read steps/<id>_prompt.md and execute its instructions. Use only these
 tools: <tools from the dispatch line>. The response protocol is inside the
 file. Reply to me with a single line starting with OK <id> or ERROR: <gist>."

# 3. Record (output = ok/error/aborted only; never look at the result body).
#    Pass back exactly the one reply line the subagent gave you — it is a
#    control fact (liveness signal), not task content.
$WFRUN record <xml> <id> --result steps/<id>_result.md --vars vars.json \
    --log steps.log --reply "<the subagent's one reply line, verbatim>"
#   ok(0) / error(1) / aborted(3) as before, plus:
#   decision(4)          -> the step asked for a ruling; see "On decision"

# If the subagent never replies, or its reply doesn't look like "OK <id>" /
# "ERROR:" (interrupted mid-turn, connection drop, garbled output): do NOT
# guess or wait indefinitely. Poll instead —
$WFRUN poll steps/<id>_handle.json
#   done(0)              -> the result file completed after all; call
#                           record now (pass --reply if you did receive
#                           something, even if it looked malformed)
#   running(10)           -> still within the step's timeout; poll again
#                           after a short pause, or keep working and check
#                           back
#   deadline-exceeded(11) -> treat as ABORTED (see "On abort" below). wfrun
#                           cannot kill the subagent (B-layer limitation) —
#                           this is a verdict, not a termination

# 4. Report one line to the user ("<id>: ok/error/aborted", progress =
#    steps.log lines / max), move on
```

### Delegating without a subagent tool

Move 2 assumes you have a subagent facility. Some harnesses do not — Pi's
built-in tool set is `read | bash | edit | write | grep | find | ls` and
nothing else. There, spawn a **fresh non-interactive agent process from
`bash`** and hand it the same fixed message. The message is unchanged: it
carries only control facts, so it is harness-neutral. Only the delivery
differs.

```bash
# Pi: one blocking call per step, from the bash tool
node <npm-prefix>/node_modules/@earendil-works/pi-coding-agent/dist/cli.js \
     -p --mode text --tools <tools, in this CLI's own names> \
     --no-session --no-skills --model <the dispatch line's resolved name> \
     "<the fixed message above, verbatim>"
```

Four things this depends on, each measured rather than assumed:

- **Go through `node` and the CLI entry, not the `pi` command.** On Windows
  `pi` is an npm `.CMD` shim whose cmd.exe layer truncates an argument at
  the first newline — silently, exit 0. The fixed message is one line so it
  would survive today, but the shim is not a safe channel and the entry it
  wraps is `node <entry> %*` anyway.
- **Pass a timeout well above the step's.** The bash tool has no default
  one. A step doing real work does not finish inside 120 s: in testing, a
  child had already written its deliverable and its result file when a
  120 s ceiling cut it off before it could reply.
- **That cut-off is survivable, by design.** A missing reply is exactly what
  `poll` is for: it returned `done`, and `record` without `--reply` then
  produced `ok` from the completed result file. Do not re-dispatch a step
  just because you did not see its reply — poll first.
- **`--tools` here is a real restriction, not advice.** Unlike the Agent
  tool, this CLI enforces the list, so a read-only step can be made
  genuinely read-only. Translate the dispatch line's names into the CLI's
  own (`Read` → `read`, `Write` → `write`, …); keep the **untranslated**
  names in the message text, which quotes the dispatch line verbatim.

## No `<parallel>` support

Both run-llm layers (this prompt-driven one and the fully-automated one)
execute sequentially only. `<parallel>` children must be run one after
another, each through the full 4-move protocol, same as a plain sequence.
Do not delegate more than one step's subagent before that step's `record`
has returned.

## Evaluating control structures

- `test=` : `$WFRUN eval "<expr>" --vars vars.json` — branch only on the
  literal `true`/`false` it prints
- `ask=` : `$WFRUN ask "<question>" --vars vars.json --quiet --log steps.log` —
  same (the reason goes straight to the log file; you do not see it). The
  dispatch string above stays fixed — do not add `--backend`: `ask` detects
  the running harness on its own (`CLAUDE_CODE_SESSION_ID` set → the claude
  CLI, unset → the pi CLI) and logs which one ran under `"backend"` in each
  steps.log entry. When it ran against the pi CLI, `cost_usd` in that entry
  is always `0.0` — a `0.0` there means "not measured", not "measured zero";
  only claude-CLI entries carry a real cost
- `while`/`each` : repeat the full 4-move protocol every iteration. Stop and
  report when the workflow's `max` is reached — count only steps.log's
  **step-execution** entries (the ones carrying a `"step"` field). `ask`
  judgments share this log file but are condition evaluations, not step
  executions, and do not consume the cap. (Layer A's `dispatch` enforces
  this same count in code, so it will refuse rather than let you overrun.)

## `<replan>` nodes (dynamic continuation, one level deep)

1. `$WFRUN prompt <xml> <replan-id> --vars vars.json --out steps/<id>_prompt.md
   --result replans/<id>.xml` — assembles the **builder** prompt (same firewall:
   you never see it). No sentinel or handle.json here — a replan's own XML
   well-formedness, checked next, already proves it is complete
2. Delegate with the same fixed message (the builder role is inside the file)
3. Validate the generated continuation **programmatically**:
   `$WFRUN validate replans/<id>.xml --as-child --defined-vars vars.json`
   - errors → re-delegate with `--fix "<the validator error lines>"`, at most
     `retry` times, then follow on-error
4. `$WFRUN plan replans/<id>.xml` — the child control skeleton; execute it with
   this same protocol, counting its steps toward `max`, then continue after the
   replan node. Generated continuations must never contain another `<replan>`
   (the validator enforces this)

## On error (authority you do NOT have)

When record returns `error`: if `retry` remains, redo from move 1 (no fix).
When exhausted, follow `on-error` (shown by plan) — `fail` = stop and report /
`ignore` = move on / `debug` = delegate a diagnosis subagent with: "You are
the debug role defined in .claude/agents/debug.md — read that file and adopt
it. Then read steps/<id>_prompt.md and steps/<id>_result.md, diagnose, answer
RETRY or FAIL, and if RETRY write fix instructions to steps/<id>_fix.md". On
RETRY, redo from move 1 with `--fix "$(cat steps/<id>_fix.md)"`, **exactly once**.
**You have no authority to read result files, debug, devise workarounds, or
fabricate substitute results.**
Stopping is not failure in this mode — every trace is on disk and the user can
decide; that is the normal terminal state.

## On decision (`record`/`wait` exit 4) — the step hit a fork it may not settle

A `DECISION:` response is **not an error**. It never enters `retry`,
`on-error`, or the abort machinery: the step did not fail, it asked a
question it is not allowed to answer alone. Answering it is never *yours* —
you have no more authority to pick a branch here than the step did. Who may
answer instead is the workflow's `decider:`, which `plan` printed for you.

`record`/`wait` already filed the request outside the step's result file (a
later `prompt --result` deletes result files, which would otherwise destroy
it) and printed both paths.

1. **`decider: human` (the default) — present the two paths and stop.** Say
   which step asked, give the request path and the answer path. Do **not**
   read the request — same firewall as everywhere else in this mode; the user
   reads it, you carry paths
   - **`decider: llm` — delegate it, the same way you delegate a step.**
     Spawn one subagent with the model `plan` printed (`decider-model=`, on
     the workflow line or the step's own), and pass it **paths only**: the
     request path and the answer path. You still do not read the request
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
   - **Cost of the rule, accepted**: on this layer an escalation reason is
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
$WFRUN record <xml> <id> --result steps/<id>_result.md --vars vars.json \
    --log steps.log --answer decisions/<id>_dNN_answer.md
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
execution. Layer A's `dispatch` widens its own per-cycle cap to match, so a
ruling never costs you a retry you needed.

**The llm decider's budget is counted for you.** Two rulings per step, after
which the verdict message says so and asks for a person; human rulings marked
`--decider human` never count. You keep no tally — the ledger in `decisions/`
is the count, and `record` reads it.

## On abort (`record` exit 3, or `poll` reports `deadline-exceeded`)

`aborted` is a different failure mode from `error` and is handled
separately, never folded into the `retry`/`on-error` machinery above: it
means the subagent went silent (interrupted, connection drop, killed
mid-turn) rather than finishing and reporting a failure. Retrying it like
an ordinary `error` would burn the step's `retry` budget on an
infrastructure event the step itself never got a chance to fail at, and
handing it to `debug` makes no sense either — there is no execution to
diagnose.

- **Re-delegate the same step exactly once**, from move 1 (`prompt` again —
  this also clears the stale result file and re-dispatches the handle with
  a fresh `dispatched_at`)
- If that re-attempt **also** aborts: stop and report to the user. Do not
  retry a second time and do not fall through to `on-error`
- This one-retry budget is independent of the step's own `retry` attribute
  (aborted attempts do not consume it, and its exhaustion does not affect
  this one-time abort allowance)

## Enforcement boundaries (what is NOT deterministic here)

**What the sentinel/handle/`poll` machinery makes deterministic:**
telling "the subagent finished writing" apart from "the file exists but is
partial/stale/never touched" no longer depends on trusting the subagent's
self-report. The completion marker is checked by code (`record`, `poll`),
not inferred from prose; a missing result file or a missing marker is a
*fact* about the filesystem, not a judgment call. `record --reply` further
cross-checks the reply channel against the file channel (reply claims OK
but the file is missing/incomplete/mismatched -> flagged, never silently
trusted either way).

**What stays advisory:** the reply-channel liveness signal itself. Nothing
forces the subagent to reply with the exact `OK <id>` / `ERROR:` format, or
forces it to reply at all before going silent — that is exactly the
scenario `poll`'s `deadline-exceeded` exists to catch from the outside.
`poll` also cannot terminate a still-running subagent (no kill authority in
this layer); it only ever renders a verdict. And nothing here defends
against you, the orchestrator, fabricating a `--reply` value that does not
match what the subagent actually said — pass it verbatim or omit it.

Two more of run-cc's structural guarantees degrade to prompt level in this
mode; know them, do not paper over them:

- **`tools=` is advisory.** The Agent tool has no per-call tool restriction,
  so the dispatch line's tools cannot be enforced on the subagent — the
  "Use only these tools" sentence in the fixed message is a likelihood lever
  only. Step `expect-file`/`schema` checks (`wfrun record`) still verify
  outcomes deterministically.
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

  The hook is a Claude Code mechanism, so this mechanical backstop only
  exists there. Outside Claude Code, `wfrun prompt --result` prints a note
  that the firewall is prompt-level only rather than searching for the
  marker (there is no `.claude/settings.json` hook system to check) —
  ⟦STEP-GATE⟧ is what carries the guarantee in that case.

---

⟦STEP-GATE⟧ Immediately before issuing each delegation, verify three conditions —
(1) your previous output quotes the path line printed by `wfrun prompt`
(2) the delegation message is the fixed text above, containing no summary or
    paraphrase of task content
(3) you have not Read the prompt, result, or vars.json for this step
If any is missing, do not delegate — perform the missing move first.
"Checking the prompt file's content", "summarizing results", and "batching
multiple steps" are not diligence or efficiency; they are protocol violations.
