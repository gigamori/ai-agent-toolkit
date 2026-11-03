# Harness reference — Pi

Read this when Step -1 resolved the harness to Pi. Pi's built-in tools are
`read | bash | edit | write | grep | find | ls`: no subagent tool and no
background commands. A host may publish a `subagent` tool on top of them;
**Route selection** below decides, before the first delegation, whether turns
go through it or through a fresh `pi -p` process started from the `bash` tool
(the CLI route). On the CLI route **P1 delegation** and **P2 time-bound** both
go through the `bash` tool.

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

## Route selection — subagent or CLI (before the first delegation)

A host may publish a `subagent` tool. Two run-level checks decide whether it
may be used at all; then the table below decides each turn, and a turn that
misses any row takes the CLI route (P1, P2). Decide before anything is
dispatched: a mismatch found after real work was sent is not a safe fallback,
and a re-run of a turn uses the route of its first attempt.

Once per run, before the first delegation:

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
exist; the turn still has to be served. Per turn, one miss puts that turn on
the CLI route:

| the turn needs | the subagent route meets it only if |
|---|---|
| the turn's `planned_override` | the schema shows an explicit-model field; nothing else carries a model |
| both recursion layers: `MODE_ORCH_DEPTH=1` in the child's environment, and no skills loaded in it | the tool's published description says both; the schema shows no environment field to set per call |
| an audit id for the attempt's `delegation_ref` | the probe's control line carries `run=<session id>`; without it there is no id to record |

Effort, tool restriction and working directory decide nothing here: this skill
passes none of them for a turn on either route. A capability that the
published schema or description does not show is unmet; do not fill it in
from memory. Record the route and the reason in the run index prose (above
the JSONL block), at the first delegation and again whenever a later turn
takes the other route. The subagent route has not been exercised on a live
host.

### The subagent route

- One `subagent` call per turn, single mode (`agent` and `task`): `task` is the
  assembled prompt, the model is `planned_override`, and the top-level `timeout`
  is the turn's value from P2's table, counted from the call.
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
- `--model` — pass `planned_override` on every turn. A rejected model → the
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
   `issue`, so launch the judge in the very next call, with P1's invocation
   shape, changing only this: `PROMPT_FILE` is the already-written
   `<S>-judge-launch-prompt.md`; the preflight diagnostic stops `blocked`
   without launching or reissuing the judge. No file-writing call is placed
   between `issue` and launch. `--model` is the model `ultra` resolves to in
   `references/execution-profiles.md`; add `--tools read,grep,write`;
   `<turn-key>-<attempt>` becomes `<turn-key>-<attempt>-judge` in the raw path
   and the stem; the `bash` `timeout` is the printed timeout. The extractor's
   stdout is the judge's reply; check it:

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

None. pi has no permission layer, so the reply contract's denial clause cannot
trigger and there is no `deny_scan.sh` counterpart. For `blocked` this means
SKILL.md's machine-detected source does not exist here; the whole weight sits
on the self-judged clause in the injected prompt, including its "unless a rule
permits it" half. Do not carry over Claude Code intuitions.

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
