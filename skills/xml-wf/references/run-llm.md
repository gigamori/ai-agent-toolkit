# LLM orchestration mode (run-llm): execute the XML as a control plane

Alternative to `--run-cc` (wfrun batch execution). Use it for interactive
sessions where the user wants to **watch each step land, grant permission,
and step in between steps**. When deterministic guarantees matter, use
`--run-cc`. (Intervening *between* steps is what this mode gives you; a step
that needs a ruling mid-task has to ask for one, which is the `DECISION:`
channel below — that works in both modes.)

This protocol is platform-independent: any agent with wfrun (Python) and a
subagent facility (the Agent tool in Claude Code; on Pi a `subagent` tool or a
fresh `pi -p` process; the equivalent subtask delegation elsewhere) can act as
the orchestrator.

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
3. **Fix `<ws>` before anything changes directory.** `<ws>` is the absolute
   path of the directory you were started in, even when that is a worktree —
   never one created later for this run. Every `<ws>` below means that path,
   wherever you work afterwards
4. Create the run dir `<ws>/tmp/runs/<name>_<ts>-llm/`. Below, `<run>` is its
   absolute path, and every run file is written under it. Keep the `-llm`
   suffix: the `vars.json` hook under "Enforcement boundaries" matches it. If
   that directory already exists, do not reuse it: take a fresh `<ts>`. Run
   `git -C <ws> check-ignore -q <run>`; if it is not ignored, say so in your
   first report and leave `.gitignore` alone. Write `<run>/vars.json` with the
   resolved `<param>` values (ask the user for missing required ones) — the
   single moment you write values
5. Create an empty `<run>/steps.log`. Every `--log` below names exactly this
   file: `prompt` counts the workflow's `max` from it

Every step runs through your own delegation facility (on Pi, "Delegating on
Pi"), by the protocol below: the steps run on the agent you are orchestrating
from, whatever else is installed. The model names on the dispatch line come from `model_map.json`'s
`llm` table — the names your execution facility accepts. (`wfrun dispatch` /
`wait`, which run a step through the claude CLI instead, are not part of this
mode.)

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
#    model=..., effort=..., tools=... — model is already runner-resolved through
#    model_map.json ("model=X (mapped from Y)" when a mapping applied): pass
#    the resolved name verbatim, never translate it yourself. A step still
#    carrying a legacy name (haiku/sonnet/opus) is NOT rebound here -- it
#    prints unresolved, since only the model= dispatch path aliases legacy
#    names (model-legacy-name warns); migrate the step's model= instead.
#    Then the attempt's own facts: attempt=cNNaNN, result=<this attempt's
#    result file>, timeout=<seconds>, session-dir=<dir>, description=<d>.
#    The prompt file already contains the full role and
#    mode text (joined into one file: the Agent tool has no system-prompt
#    input, unlike run-cc's --append-system-prompt), so any generic subagent
#    will do; apply model from the dispatch line when delegating. effort=
#    cannot be applied: the Agent tool has no effort input, and the Pi
#    delivery below passes none
$WFRUN prompt <xml> <id> --vars <run>/vars.json \
    --out <run>/steps/<id>_prompt.md --result <run>/steps/<id>_result.md
#   [--fix "..."]          only as "On error" says
#   [--new-cycle]          only as "Cycles" below says
#   [--redispatch "..."]   only as "On abort" says

# 2. Delegate (this fixed message is ALL you hand to the subagent; when the
#    dispatch line shows tools=..., include the second sentence verbatim with
#    that list — tool names are control facts, not task content)
"Read <run>/steps/<id>_prompt.md and execute its instructions. Use only these
 tools: <tools from the dispatch line>. The response protocol is inside the
 file. Reply to me with a single line starting with OK <id> or ERROR: <gist>."
#    On Claude Code, run the Agent call in the background and give it the
#    dispatch line's description= value verbatim as its description. It is
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
#    arrives after the attempt is recorded is stale: ignore it. On Pi there is no
#    watch: the call blocks, and its timeout is the deadline ("Delegating on
#    Pi")

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
#    steps.log lines / max), move on
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

Move 2 assumes you have a subagent facility. Pi's built-in tool set is
`read | bash | edit | write | grep | find | ls` and nothing else; a host may
publish a `subagent` tool on top of it. The message is the same fixed one on
either route: it carries only control facts, so it is harness-neutral. Only the
delivery differs.

**Choose the route before the first delegation.** Two run-level checks decide
whether the `subagent` tool may be used at all; then the table below decides
each step, and a step that misses any row takes the CLI route. Decide before
anything is dispatched: a mismatch found after real work was sent is not a safe
fallback, and the same step is not sent again through the other route unless
its first attempt's stop is confirmed.

Once per run:

1. **Schema.** The published `subagent` tool's schema has a top-level `timeout`
   (seconds). The tool's name alone is not this check.
2. **Probe.** Call the tool once with a top-level `timeout` and a task that
   touches nothing (reply with the word OK), naming an agent whose definition
   grants no write or shell tool. The first line of the returned content starts
   with `[subagent] outcome=` and the outcome is `completed`. A call without
   `timeout` may legitimately lack that line, so an untimed probe shows nothing
   either way. No such agent to name is a failed probe.

Either check failing, or the tool being absent or unreadable, puts the whole run
on the CLI route. Both checks say only that a deadline and a result notice
exist; the step still has to be served. Per step, one miss puts that step on the
CLI route:

| the step needs | the subagent route meets it only if |
|---|---|
| the dispatch line's model | the schema shows an explicit-model field; nothing else carries a model |
| the dispatch line's `tools=` (when it shows one) | the host lists an agent definition whose frontmatter `tools:` is exactly that list; the schema shows no per-call tool restriction, and the sentence in the message is not one |
| a child that cannot start another orchestrator | the tool's published description says its children load no skills |
| an audit id for the attempt | the probe's control line carries `run=<session id>`; without it there is no id to record |

`effort=` and a working directory are passed on neither route. A capability
that the published schema or description does not show is unmet; do not fill it
in from memory. Say which route you chose, and why, in your first report to the
user, and again whenever a later step takes the other route. The subagent route
has not been exercised on a live host.

**The subagent route.** One `subagent` call per step, single mode (`agent` and
`task`): `task` is the fixed message, the model is the dispatch line's resolved
name, and the top-level `timeout` is the dispatch line's `timeout=`. The first
line of the returned content is the tool's control line
(`[subagent] outcome=...`), not the step's reply: split it off, give its `run=`
id in the step's report line (move 4), and pass only the child's one reply line
to `record --reply`. `outcome=completed` without `unconfirmed_descendants` is
only a candidate: `record` still decides from the result file. Any other outcome
(`stopped`, `stop_unconfirmed`, `error`, `not_started`), `completed` with
`unconfirmed_descendants`, or no control line is no reply: poll once, and on
anything but `done` go to "On abort".

**The CLI route.** One blocking call per step from the `bash` tool.

<!-- pi-cli-launch-contract:begin -->
**Pi CLI launch contract.** The CLI route starts a fresh, non-interactive pi
process from one `bash` call. These rules are common to every skill that does
it; the options each skill adds follow the block.

- **Entry.** `<pi-cli-entry>` is
  `<npm prefix -g>/node_modules/@earendil-works/pi-coding-agent/dist/cli.js`.
  Resolve it once per run with `npm prefix -g`; a failed command stops the run.
  Never search for the file.
- **Straight to node.** Launch `node <pi-cli-entry>`. Never `pi`, `pi.CMD`,
  `pi.ps1` or any shell-resolved launcher: the cmd.exe layer of the npm shim
  cuts the argument at its first newline, with exit 0 and no stderr.
- **Non-interactive, message on argv.** `-p`, then the message as the positional
  argument. Never `@<file>`: pi treats the include as content to reason about
  and may refuse it as prompt injection.
- **Stdin closed.** `< /dev/null`, always. pi reads stdin whenever it is not a
  TTY (`readPipedStdin()`, no timeout): an open stdin hangs the child with empty
  stdout and stderr, and a stdin that carries bytes is prepended to the prompt
  with no separator.
- **No skills.** `--no-skills`. Never `--skill`: a child that can load a skill
  can start the next orchestrator.
- **Transcript kept.** `--session-dir <session-dir>` keeps the child's session
  on disk, out of the real session store. A skill's own text names any launch
  that keeps none (`--no-session`).
- **Model.** `--model <resolved name>` on every launch: pi has no inherited-model
  path.
- **Git Bash.** Put `MSYS_NO_PATHCONV=1` in front: a positional argument that
  begins with a POSIX-looking path is otherwise rewritten. Never
  `MSYS2_ARG_CONV_EXCL='*'`, which breaks node's own path. Write every path
  handed to node in Windows form (`C:/...`). Assign the message to a shell
  variable in its own statement first and pass `"$PROMPT"`: an inline
  multi-line message dies at bash parse time.
- **Deadline.** Pass the `bash` call's `timeout` (seconds) explicitly: the tool
  has no default. When it passes, the tool kills the call's whole process tree.
  That kill is the stop request; it does not confirm the stop.
- **Record the processes.** Run the CLI in the background of the same call, take
  its `$!`, translate that and the shell's own `$$` to Windows PIDs with
  `/proc/<pid>/winpid`, and hand both to the skill's recorder before the call
  waits for the CLI: a stop is confirmed only against the processes recorded
  here. A recorder that fails must not stop the launch; that attempt then can
  never confirm a stop. Keep the call several statements long: when the whole
  call is one command, bash can replace itself with it, and `$$` then names
  that command instead of the shell.
<!-- pi-cli-launch-contract:end -->

What this skill adds to the contract:

- `--mode text`, and `--tools <the dispatch line's tools, in this CLI's own
  names>` — **a real restriction, not advice.** Unlike the Agent tool, this CLI
  enforces the list, so a read-only step can be made genuinely read-only.
  Translate the dispatch line's names into the CLI's own (`Read` → `read`,
  `Write` → `write`, …) and pass them as one comma-separated argument without
  spaces: `tools=Read,Write` is `--tools read,write`. A space-separated list
  grants only its first name. Keep the **untranslated** names in the message
  text, which quotes the dispatch line verbatim.
- `--session-dir <the dispatch line's session-dir>` keeps the attempt's
  session, which is the transcript `wfrun judge` lists as evidence after an
  abort. A line that shows no session-dir (a `<replan>` builder, a judge) is
  delivered with `--no-session` instead.
- `--model <the dispatch line's resolved name>`.
- **The bash call's `timeout` is the dispatch line's `timeout=`.** That timeout
  is the attempt's deadline: the kill of the CLI's process tree it sets off is
  the stop request, and "On abort" goes on from there. Do not shorten
  it: in testing, a 120 s ceiling cut off a child that had already written its
  deliverable and its result file before it could reply.
- The recorder is `$WFRUN pids <handle> <pid> <pid>`: it records the two
  Windows PIDs as (PID, image, creation time) of the attempt, once, and refuses
  when a PID is not a running process or the attempt already has a record or a
  stop. It is what lets `wfrun stop --facet pi-cli` confirm a stop.
- The recorder needs `/proc/<pid>/winpid`, which Git Bash provides on Windows.
  Elsewhere it records nothing, no stop is confirmed, and an aborted step is
  never restarted.
- A judge has no recorder: its handle is not an attempt's, so `$WFRUN pids` and
  `wfrun stop` refuse it. Launch it with the invocation shape below, without the
  `$WFRUN pids` statement ("On abort", 5).

Invocation shape (the call's stdout is the child's reply):

<!-- pi-cli-launch-line -->
```
PROMPT='<the fixed message above, verbatim>'
MSYS_NO_PATHCONV=1 node <pi-cli-entry> -p --mode text \
   --tools <the dispatch line's tools in this CLI's own names, comma-separated> \
   --no-skills --session-dir <the dispatch line's session-dir> \
   --model <the dispatch line's resolved name> "$PROMPT" < /dev/null &
child=$!
$WFRUN pids <run>/steps/<id>_handle.json \
   "$(cat /proc/$child/winpid)" "$(cat /proc/$$/winpid)" \
   > /dev/null || echo "processes not recorded" >&2
wait $child
```

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
  only claude-CLI entries carry a real cost
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
2. Delegate with the same fixed message (the builder role is inside the file)
3. Validate the generated continuation **programmatically**:
   `$WFRUN validate <the result= file> --as-child --defined-vars <run>/vars.json`
   - errors → re-delegate from 1 with `--fix "<the validator error lines>"`;
     `prompt` admits that at most `retry` times, then refuses and names the
     on-error to follow. It refuses any re-delegation while the last
     attempt's file is missing or is not well-formed XML: whether that builder
     stopped is unknown then — stop and report
4. `$WFRUN plan <the result= file>` — the child control skeleton; execute it
   with this same protocol, counting its steps toward `max`, then continue
   after the replan node. Generated continuations must never contain another
   `<replan>` (the validator enforces this)

## On error (authority you do NOT have)

When record returns `error`, run move 1 again for the same step, without
`--fix`. `prompt` applies run-cc's retry/debug rules in code: it starts the
next attempt while `retry` remains, and otherwise refuses with what `on-error`
(shown by plan) leaves — `fail` = stop and report / `ignore` = move on (a
later iteration of the same step then takes `--new-cycle`) / `debug` =
delegate a diagnosis subagent, once, with: "You are the debug role defined in
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
     `<request path> (decider, model=..., answer=<answer path>, description=...)`.
     Spawn one subagent with that model — on Claude Code, with that
     description — and pass it **paths only**: the request path and the
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
again: stop and report. In order:

1. **Check for completion.** The watch's `deadline-exceeded`, or `poll` after
   a call that ended without a valid reply, is this check. `done` → record
   it; nothing below applies
2. **Request the stop, right after that check.** On Claude Code, TaskStop the
   step's Agent task — unless its completion notice already arrived, which
   leaves nothing to stop. On Pi the call's timeout already requested it (the
   CLI route's `bash` timeout, the subagent route's `timeout`), and a call that
   returned before its deadline has ended
3. **Observe the stop:**

   ```bash
   $WFRUN stop <run>/steps/<id>_handle.json --facet cc --task-stop <stopped|completed|failed>
   $WFRUN stop <run>/steps/<id>_handle.json --facet pi-cli
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
   `stop` watches. On Claude Code it confirms only when TaskStop reported the
   task stopped or already ended, the transcripts of the subagent and of
   every agent it started are found, none of them grew during the window,
   and none of them holds a tool call without a result, one that started
   work in the background, or one refused or interrupted. A TaskStop that
   interrupts a running call can leave it recorded as refused: the stop then
   stays unconfirmed, and `record` still records the attempt `aborted`, with
   the refusal check's verdict on a `deny-check:` line and in `steps.log`: a
   stop is not a denial. On the Pi CLI it confirms only when every process
   recorded at the attempt's launch is gone, which is why the launch line
   records them. The `subagent` route has
   no facet here: its stop is not confirmed, so record the attempt, then stop
   and report. Neither facet looks at processes outside what it watches, nor
   at anything done outside files; the stop record names what it did not
   observe
4. **Record** the attempt — move 3's command without `--reply`, whatever
   reply arrived: the attempt is settled by its file and its stop, so
   `aborted` (3) follows, and `record` reads no reply after a stop that ended
   without a completion. A refusal it prints instead ends in what to do
5. **Issue the judge** (a confirmed stop only):

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
   single line it asks for." On Claude Code, give the Agent call the printed
   description, run it in the background, and in the same message start
   `$WFRUN poll <judge handle> --wait` as its deadline watch; on the Pi CLI,
   use the invocation shape above with this message as `PROMPT`,
   `--tools read,grep,write`, `--no-session` in place of `--session-dir`, the
   printed model, the printed timeout as the `bash` call's `timeout`, and no
   `$WFRUN pids` statement (a judge's handle is not an attempt's: a judge is
   bounded by its timeout alone, never observed or restarted). A judge that
   has not replied by its deadline, or
   whose reply is not the one line `allow <record>` / `deny <record>`, is a
   refusal: TaskStop it if it still runs, then stop and report. A judge is
   never delegated twice
6. **Restart, or stop.** `deny <record>` → stop and report. `allow <record>` →
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
the Pi call's timeout (CLI or `subagent` route), and `stop` only observes what
each facet lets it see. And nothing here defends against you, the orchestrator, fabricating a
`--reply` value that does not match what the subagent actually said — pass it
verbatim or omit it. The same holds for the judge: `prompt` checks its
record's form, citations, checksums and timing, not who wrote it.

Three more of run-cc's structural guarantees degrade in this mode; know
them, do not paper over them:

- **`tools=` is advisory.** The Agent tool has no per-call tool restriction,
  so the dispatch line's tools cannot be enforced on the subagent — the
  "Use only these tools" sentence in the fixed message is a likelihood lever
  only. Step `expect-file`/`schema` checks (`wfrun record`) still verify
  outcomes deterministically. The judge's tool list is advisory there too;
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

⟦STEP-GATE⟧ Immediately before issuing each delegation, verify three conditions —
(1) your previous output quotes the path line printed by `wfrun prompt` (for a
    judge, by `wfrun judge`; for an llm decider, by `wfrun decide`)
(2) the delegation message is the fixed text above, containing no summary or
    paraphrase of task content
(3) you have not Read the prompt, result, or vars.json for this step
If any is missing, do not delegate — perform the missing move first.
"Checking the prompt file's content", "summarizing results", and "batching
multiple steps" are not diligence or efficiency; they are protocol violations.
