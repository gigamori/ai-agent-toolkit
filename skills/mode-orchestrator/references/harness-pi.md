# Harness reference — Pi

Read this when Step -1 resolved the harness to Pi. Pi's built-in tools are
`read | bash | edit | write | grep | find | ls`: no subagent tool and no
background commands. A host may publish a `subagent` tool on top of them;
**Route selection** below decides, before the first delegation, whether that
native route is available. A v2 run delegates only through it — the restart
judge included. A fresh `pi -p` process started from the `bash` tool (the CLI
route; **P1 delegation** and **P2 time-bound** go through the `bash` tool)
is described below for its stop-observation mechanics and recorded runs, and
is not a fallback.

## P0 — Paths (resolve once, before the first delegation)

Two absolute paths under two different, non-interchangeable roots. Resolve each
with its own command, record both in the run index (prose above the JSONL
block), reuse them for every turn. A failed command → `blocked`; never search
for the file.

- `<pi-cli-entry>` — root = **the npm global prefix**: `npm prefix -g`, joined
  with `node_modules/@earendil-works/pi-coding-agent/dist/cli.js`.
- `<pi-reply>` — root = **the agent dir**:

```
node -e "console.log(require('path').resolve(process.env.PI_CODING_AGENT_DIR || require('os').homedir() + '/.pi/agent', 'skills/mode-orchestrator/scripts/pi_reply.js'))"
```

  The output is the whole path in platform form and honors
  `PI_CODING_AGENT_DIR` (the shadow-agent-dir lever in
  `docs/skills/mode-orchestrator/AUTHORING_CONTRACT.md`). Never hard-code
  `~/.pi/…`: under `MSYS_NO_PATHCONV=1` the tilde form fails with
  `Cannot find module 'C:\c\Users\…'`.

Every other helper script of this skill sits in the same directory as
`<pi-reply>`; `<pi-scripts>` below means that directory. It is not a third
resolution.

## Route selection — native availability (before the first delegation)

A host may publish a `subagent` tool. One run-level availability read and one
schema check decide whether a run may delegate at all; then the table below
decides each turn. A turn that misses a row does not move to the CLI route:
the run stops `blocked`, naming the unmet capability. Decide before anything
is dispatched: a mismatch found after real work was sent is not a safe
fallback.

Once per run, before the first delegation:

1. **Availability.** Call the read-only `delegation_contract` tool with
   `{"operation":"observe"}`. It returns the native route's
   `available|unavailable` with a reason string (delegation disabled by
   configuration, profile invalid, tool absent), the constraints it enforces,
   and the per-effort setting states. `unavailable` is `blocked` before any
   delegation, quoting the reason. No permission is read or computed: whether
   a `tool_call` handler (taskflow's, for one) is registered, or an extension
   failed to load, decides nothing — a refusal is decided per call at the
   host's boundary and classified afterwards (the contract's host-result
   table). If the session has no `delegation_contract` tool, the running build
   predates the contract and cannot pin a model → `blocked`, naming the old
   build. A build that still answers in the earlier vocabulary (overall
   `allow|deny|unknown`, route states `permitted|unsupported|denied|unknown`;
   pi-extensions `ea1007a` / pi-studio `52a0444`) is read only for its native
   route state: `permitted` counts as available; any other value is treated
   as `unavailable` and quoted as that build's answer (a transitional stop, no
   worse than before). The CLI entry of any answer is ignored. A tool being
   present is never itself a ground, and no live agent is launched to
   discover any of this.
2. **Schema.** The published `subagent` tool's schema has a top-level `timeout`
   (seconds); contract builds also expose `expectedResolution` (the pinned
   binding field) and publish the per-child delegation evidence. The tool's
   name alone is not this check. The published schema and description are
   the pre-call grounds; the tool being absent or unreadable is `blocked`.

Per turn, a miss in the table below is `blocked` for the run:

| the turn needs | the subagent route meets it only if |
|---|---|
| the turn's approved binding | the schema shows `expectedResolution` (resolve first with `delegation_contract` `{"operation":"resolve"}` — exactly one `request.model` or `request.effort`, plus `requiredEffort` — and pass the returned `resolutionRef`, omitting `model`/`effort`); nothing else carries a model |
| both recursion layers: per-call recursion suppression in the child, and no skills loaded in it | the observe result's enforced constraints (recursion, skill discovery) cover both; do not fill either in from memory |
| an audit id for the attempt's `delegation_ref` | the published output contract shows the evidence lines (`[subagent] evidence=…`, and the conditional `run=` control line); the first real call confirms it |

Effort, tool restriction and working directory decide nothing here: this skill
passes none of them for a turn on either route. A capability that the
published schema or description does not show is unmet; do not fill it in
from memory. Record the route and the reason in the run index prose (above
the JSONL block), at the first delegation and again whenever a later turn
takes the other route. The subagent route has not been exercised on a live
host under this contract.

### The subagent route

- One `subagent` call per turn, single mode (`agent` and `task`): `task` is the
  assembled prompt, the binding is the approved `expectedResolution` (resolve,
  approve, then pass the returned `resolutionRef` and omit `model`/`effort`),
  and the top-level `timeout` is the turn's value from P2's table, counted from
  the call. On builds without the published contract there is no pinning field:
  the route is unmet (table above) and the run has already stopped at step 1
  — a call is never made with an unpinned free-form model.
- The first line of the returned content is the tool's control line, not part
  of the turn's reply. Split it off. Read the turn's status from the child's own
  text, by Execution step 3, only when the control line says
  `outcome=completed` and shows no `unconfirmed_descendants`. Any other outcome
  (`stopped`, `stop_unconfirmed`, `error`, `not_started`), `completed` with
  `unconfirmed_descendants`, no control line, or one that does not parse, is a
  turn that reported nothing: `aborted`.
- This route has no stop observer, so an `aborted` attempt on it has an
  `UNCONFIRMED` stop and Execution step 4's restart gate refuses it:
  `needs-human`. The control line's outcome goes in the run index prose (above
  the JSONL block); its `run=` id is the attempt record's `delegation_ref`.

## P1 — Delegation (CLI route)

A v2 run never dispatches through this route, and the restart judge does not
use it either (Route selection). The mechanics below remain for the restart
gate's stop-observation tooling, for reading recorded runs, and for any future
pinned CLI launcher the host may publish.

A blocking, non-interactive `pi -p` call from the `bash` tool; the assembled
prompt is the **positional message argument**.

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

- `MODE_ORCH_DEPTH=1` — recursion guard, layer 1. Step -1 stops on it. Pi's
  `bash` tool passes `process.env` to children (verified end-to-end).
- `--no-skills` is layer 2. Both layers are required; an unbounded recursive Pi
  subagent swarm has happened.
- `--model` — pass the turn's approved binding on every launch. A rejected model → the
  extractor does not run → missing status → `aborted`; the CLI had started, so
  the restart gate decides (`aborted` handling below).
- `--mode json` — event JSONL, one object per line: `session`, `agent_start`,
  `turn_start`, then `message_start` / `message_update`* / `message_end` per
  message, then `turn_end`, `agent_end`, `agent_settled`. `pi_reply.js` takes
  the **last** `turn_end` (one per tool round), requires
  `.message.stopReason === "stop"`, concatenates its `{"type":"text"}` blocks
  in order, and drops `thinking`. `.message.usage` carries
  `{input, output, cacheRead, cacheWrite, totalTokens, cost:{...,total}}`.
- `--session-dir <run-dir>/sessions` keeps the child transcript on disk (Execution
  step 4's promise). Never `--no-session`.
- The recorder is `stop_observe.js pids` (its header says what it records and
  when it refuses). `<stem>` is `<run-dir>/<turn-key>-<attempt>`, one per
  attempt, in the existing run directory. It is silent on success. A stop is
  observed against this record (`aborted` handling below).

**Prepare the message before this call.** Use the file-writing tool, not a
shell heredoc, to save the assembled prompt as UTF-8 at
`<run-dir>/<turn-key>-<attempt>-prompt.md`. Use that attempt's own path for
normal, recovery, decision and re-run turns; never overwrite an earlier
attempt's file. Retain it with the run. The judge's launch message has a
separate path, specified below. This short call reads the file into the
positional argument; it does not use Pi's `@file` include or stdin. Keeping
only the path in the command avoids truncating a long Git Bash `-c` script;
it does not remove the OS limit on the later node argument vector.

A failed write stops the run `blocked` before calling the shell. In the call,
read failure or empty content exits 64 with the fixed diagnostic prefix
`mode-orchestrator: prompt preflight failed`. Treat that diagnostic as
`blocked`, not `aborted`: record the path, failure stage and actual diagnostic
in run index prose, then stop. There is no delegation, attempt record, stop
observation, judge or retry. This takes precedence over the direct re-run
rule for commands rejected before launch. Do not classify an arbitrary exit
64 without that prefix as this preflight failure. Once the CLI starts, its
failures keep the existing `aborted` handling.

Invocation shape (substitute the P0 values; do not re-derive them):

<!-- pi-cli-launch-line -->
```
PROMPT_FILE="<run-dir>/<turn-key>-<attempt>-prompt.md"
PROMPT=$(< "$PROMPT_FILE") || {
  printf 'mode-orchestrator: prompt preflight failed: cannot read %s\n' "$PROMPT_FILE" >&2
  exit 64
}
[[ -n "$PROMPT" ]] || {
  printf 'mode-orchestrator: prompt preflight failed: empty %s\n' "$PROMPT_FILE" >&2
  exit 64
}
MSYS_NO_PATHCONV=1 MODE_ORCH_DEPTH=1 node <pi-cli-entry> -p --mode json \
   --model <the turn's resolved model> \
   --no-skills --session-dir <run-dir>/sessions "$PROMPT" \
   < /dev/null > <run-dir>/raw/<turn-key>-<attempt>.jsonl &
child=$!
node <pi-scripts>/stop_observe.js pids --stem <run-dir>/<turn-key>-<attempt> \
   "$(cat /proc/$child/winpid)" "$(cat /proc/$$/winpid)" \
   || echo "processes not recorded" >&2
wait $child \
   && node <pi-reply> <run-dir>/raw/<turn-key>-<attempt>.jsonl
```

- **You read only the extractor's stdout** — the reply text. The `--mode json`
  stream is megabytes per run and overflows the `bash` result budget into a
  temp log; never parse it in a turn.
- `<run-dir>/raw/` and `<run-dir>/sessions/` are created with the run
  directory; `>` does not create directories. The raw path is the attempt's
  `delegation_ref`; its first `session.id` joins it to the child transcript.
- `wait $child` hands the CLI's exit status to `&&`: a CLI that failed never
  reaches the extractor.
- Extractor exit non-zero → one stderr line, empty stdout → no `status:` line
  → `aborted` via Execution step 4. Exit 3: no `turn_end`. Exit 4: the terminal
  `turn_end` did not stop cleanly (`stopReason` / `errorMessage` named). Exit 2:
  usage or read error. Tests: `scripts/pi_reply_test.sh`.

Cold start is ≈15 s per one-shot `pi -p`; a turn under a minute old is not
stalled.

## P2 — Time-bound (the bash tool's native `timeout`)

Always pass `timeout` (seconds) explicitly on the `bash` call — there is no
default; the maximum is `MAX_TIMEOUT_MS = 2_147_483_647`.

| turn mode | `timeout` (seconds) |
|---|---|
| `survey` | 7200 |
| `plan` | 4800 |
| `execute` | 4800 |
| `debug` | 1800 |
| `review-process` | 1800 |
| `review-dev` | 3600 |
| anything else | 1800 |

- `review-doc` deliberately has no row (a ruling): it takes `anything else`.
  Reopen only if a real review-doc turn times out; then add a row here and in
  `scripts/watchdog.sh` together.
- An inserted decision turn runs as `review-dev` → 3600. A `--decider=human`
  wait makes no call.
- A re-run passes the seconds on its gate's `ALLOW` line instead (Re-run
  keying).
- These are the CC watchdog's `DEADLINE_*` values verbatim; re-verify against
  `scripts/watchdog.sh` whenever either side changes. They are numbers, not a
  starting point to re-derive per run.
- Never tighten below the CC values: CC's watchdog only reports, Pi's `timeout`
  kills the process tree, and this single deadline carries both CC's
  `DEADLINE_*` and its `STALL=600` job.
- On timeout, `killProcessTree` runs and the tool result is the string
  `Command timed out after <N> seconds`, verbatim.
- No `STALL` equivalent and no separate completion wake are needed: the
  orchestrator is blocked on the call, and a return without the timeout error
  means the turn is complete.
- Nothing arrives late here: P2 *is* the `bash` call, so the orchestrator holds
  either the reply or the timeout, never one after the other. The completion
  check after the stop (`aborted` handling) is the last look at the turn. No
  grace window.

## `aborted` handling (Execution step 4) — CLI route

A v2 run does not reach this section. Its turns go through the subagent
route, whose `aborted` attempt has an `UNCONFIRMED` stop and ends
`needs-human` ("The subagent route" above), so on this facet the restart gate
never issues a judge. The moves below, including the native judge launch in
move 4, apply to recorded CLI-route runs and to a subagent route that one day
gains a stop observer; `issue` still takes the CLI raw stream (`--raw`), which
the subagent route does not produce. Until both change, no judge runs here.

The moves of `SKILL.md`'s restart gate on this route, after a call that
returned `Command timed out after <N> seconds`, or one whose extractor gave no
reply ending in a well-formed `status:` line. `<S>` is the attempt's stem,
`<run-dir>/<turn-key>-<attempt>`. Read only what the scripts and the extractor
print: the child's partial output stays on disk under `--session-dir`; record
the session path in the run index.

1. **Stop request** — already made: the timeout killed the call's process
   tree, and a call that returned before its timeout has ended.
2. **Observe the stop.** It blocks for at most 60 s, plus up to 20 s when the
   check of a recorded process, begun before the window ended, has not returned:

   ```
   node <pi-scripts>/stop_observe.js pi-cli --stem <S> --cause <deadline|ended> --deadline <the timeout passed on the call>
   ```

   `--cause deadline` after the timeout message, `ended` otherwise.
   `CONFIRMED` (exit 0) only when every process the launch line recorded is
   gone. An attempt whose launch recorded none (`processes not recorded`) is
   `UNCONFIRMED`, and so is anything but the word `CONFIRMED`. A verdict
   missing from the tool result: run the same command again — the first
   verdict answers every later call.
3. **Completion check**, after a timeout: `node <pi-reply>
   <run-dir>/raw/<turn-key>-<attempt>.jsonl`. A reply ending in a well-formed
   `status:` line means the turn finished before the kill: classify it by
   Execution step 3 and record the timeout as superseded. Otherwise the turn is
   `aborted`.
4. **Gate.** No denial check exists here. `UNCONFIRMED`, or a turn key that
   already ran again → no restart, `needs-human`. Otherwise, before issuing
   the judge, save this fixed launch message with the file-writing tool as
   UTF-8 at `<S>-judge-launch-prompt.md`:

   `Read <S>-judge-prompt.md and execute its instructions. Use only these tools: read, grep, write. Reply to me with the single line it asks for.`

   `<S>-judge-prompt.md` is the instruction file `issue` will create, not the
   launch message: never write over it. A launch-message write failure stops
   `blocked` without issuing a judge. Once the message is saved, issue once:

   ```
   node <pi-scripts>/judge_gate.js issue --stem <S> --deliv <the attempt's deliverable path, or -> --raw <run-dir>/raw/<turn-key>-<attempt>.jsonl --session-dir <run-dir>/sessions
   ```

   It prints one line, `<prompt> (judge, model=ultra, tools=Read,Grep,Write,
   record=<record>, timeout=<seconds>, description=<d>)`; anything else (a
   refusal, exit 1; exit 64 or 70) is `needs-human`. The timeout counts from
   `issue`, so launch the judge in the very next call, through the subagent
   route ("The subagent route" above): one `subagent` call whose `task` is the
   already-written launch message (the content of `<S>-judge-launch-prompt.md`),
   binding the `ultra` policy class's model — resolved and approved before
   `issue` under the delegation contract's source priority, the profile row
   being the default — by passing the approved `resolutionRef` as
   `expectedResolution`, with `timeout` the printed timeout. Never the CLI. If
   the model resolution, approval or launch preparation is not complete before
   `issue`, do not issue: stop. No file-writing call is placed between `issue`
   and launch. The judge's tool restriction (read, grep, write) is in the
   launch message only; the host does not enforce it. Split off the control
   line as above; the rest of the child's text is the judge's reply; check it:

   ```
   node <pi-scripts>/judge_gate.js check --stem <S> --reply "<the judge's reply, verbatim>"
   ```

   `ALLOW <seconds> <record>` (exit 0) → re-run, keyed as below. `REFUSED`
   (exit 1), no verdict word, or a judge call that timed out or gave no reply
   → `needs-human`. Record one line in the run index prose: the judge's raw
   path, the check's output line, the record path.

### Re-run keying

- A re-run is the next attempt number of the same turn key: the same assembled
  prompt, except its deliverable line, which names the attempt's own path
  (Execution step 4; the `attempt` record's `file`); its own raw path and
  stem; the `bash` `timeout` from the `ALLOW` line. No
  keyed watchdog exists, and nothing of an earlier attempt is overwritten: raw
  paths and stems are numbered per attempt, child sessions are UUID-named.
- A prompt-file write failure or the preflight diagnostic is terminal
  `blocked` as P1 specifies; it never takes this direct re-run path.
- A command the shell rejected started nothing: record it
  (`reported_status: null`, `effective_status: "aborted"`, `file: "-"`) and
  re-run it directly as the next attempt number (Execution step 4), with the
  table's `timeout`. Re-issuing under the same key makes two commands name one
  attempt (`U-COMMAND-ATTEMPT` in the adaptive audit).

## `--decider=human` waiting

Works as on Claude Code with nothing built: the orchestrator blocks on `bash`
only during a delegation, so ending its turn at a step boundary presents the
request — an interactive `pi` session continues on the answer, `pi -p` ends the
run (the intended degradation). Unrelated to the `-p` on the delegation line:
every delegated turn is headless and never waits.

## Denial check

None. Core pi has no permission layer of its own. A host such as Pi Studio
adds guards (shell guard, workspace sandbox, permission gate, protected paths)
that a native child also carries, and an extension may refuse a call from a
`tool_call` handler — but none of them publishes a transcript form this skill
can scan, so there is no `deny_scan.sh` counterpart. For `blocked` this means
SKILL.md's machine-detected source does not exist here; a refusal reaches the
run only as a refused call or in the child's own report, and otherwise the
weight sits on the self-judged clause in the injected prompt, including its
"unless a rule permits it" half. Do not carry over Claude Code intuitions.
There is no pre-delegation permission check either: refusals are classified
afterwards (the contract's host-result table). Route selection step 1 reads
availability, not permission.

## Residual risks specific to this harness

- `timeout` kills a legitimately slow turn exactly as hard as a hung one; there
  is no `STALL`-style middle ground. Size generously per mode.
- Both recursion layers live on the same command line; a delegation written
  without them defeats the guard at once, and nothing outside that string
  enforces them.
- A `CONFIRMED` stop speaks only for the processes the launch line recorded
  (the CLI's node and the shell around it): their descendants, detached or
  not, and anything done outside files are not observed. That the recorded
  node is the process doing the work is unverified on a real host.
- The host contract is published (pi-extensions `ea1007a`, `fd9a789`;
  pi-studio `52a0444`) in a version whose `observe` still computes a
  permission state that reads `unknown` under any `tool_call` handler. A
  request to the host asks it to publish native `available|unavailable` with a
  reason instead; until a build answers that, Route selection step 1
  treats a non-`permitted` native state as unavailable and a run in such a
  session stops before delegating. Live completion of the R2 contract on this
  facet is not claimed. Further published limits to carry into any claim: the
  launch-time payload guard holds only for providers honoring the before-send
  payload callback (not a universal transport boundary), and the unpinned
  legacy model path still expands aliases/prefixes — v2 runs never use it,
  since the binding must be pinned.
- A Pi child of a taskflow-enabled session runs without taskflow's `tool_call`
  refusals (its `child-ledger` mode registers none), so a refusal such as the
  project-notes scan denial does not apply to it. The invariant "a child is not
  wider than the parent" has this one known exception, tracked by the taskflow
  project.
