# Harness reference — Pi

Read this when Step -1 resolved the harness to Pi. Pi has no subagent tool and
no background commands — its built-in tools are
`read | bash | edit | write | grep | find | ls` — so **P1 delegation** and
**P2 time-bound** both go through the `bash` tool.

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

## P1 — Delegation

A blocking, non-interactive `pi -p` call from the `bash` tool; the assembled
prompt is the **positional message argument**.

Rules — each is a measured failure mode, not a preference:

- Never deliver the prompt as `@<file>`: pi treats the include as content to
  reason about and may refuse it as prompt injection.
- Never invoke `pi.CMD` / `pi.ps1` or any shell-resolved launcher: the cmd.exe
  layer truncates argv at the first newline (exit 0, no stderr). Go straight to
  `<pi-cli-entry>`.
- From an MSYS shell (Git Bash) set `MSYS_NO_PATHCONV=1`: a positional argument
  beginning with a POSIX-looking path is otherwise rewritten
  (`/mode-orchestrator …` → `C:/Program Files/Git/mode-orchestrator …`).
  Never `MSYS2_ARG_CONV_EXCL='*'` (it breaks node's own path). Write every path
  handed to node in Windows form.
- Assign the prompt to a shell variable in its own command first and pass
  `"$PROMPT"`; an inline multi-line prompt dies at bash parse time.

Invocation shape (substitute the P0 values; do not re-derive them):

```
MODE_ORCH_DEPTH=1 node <pi-cli-entry> -p --mode json \
   --model <the turn's resolved model> \
   --no-skills --session-dir <run-dir>/sessions "$PROMPT" \
   < /dev/null > <run-dir>/raw/<turn-key>-<attempt>.jsonl \
 && node <pi-reply> <run-dir>/raw/<turn-key>-<attempt>.jsonl
```

- **You read only the extractor's stdout** — the reply text. The `--mode json`
  stream is megabytes per run and overflows the `bash` result budget into a
  temp log; never parse it in a turn.
- `<run-dir>/raw/` and `<run-dir>/sessions/` are created with the run
  directory; `>` does not create directories. The raw path is the attempt's
  `delegation_ref`; its first `session.id` joins it to the child transcript.
- Extractor exit non-zero → one stderr line, empty stdout → no `status:` line
  → `aborted` via Execution step 4. Exit 3: no `turn_end`. Exit 4: the terminal
  `turn_end` did not stop cleanly (`stopReason` / `errorMessage` named). Exit 2:
  usage or read error. Tests: `scripts/pi_reply_test.sh`.
- `< /dev/null` is mandatory. pi reads stdin unconditionally whenever it is not
  a TTY (`readPipedStdin()`, no timeout): an open stdin hangs the child silently
  with empty stdout and stderr; a stdin carrying bytes is prepended to the
  prompt with no separator, so the turn silently runs a different prompt. Only
  a startup-failure measurement arm leaves stdin inherited.
- `MODE_ORCH_DEPTH=1` — recursion guard, layer 1. Step -1 stops on it. Pi's
  `bash` tool passes `process.env` to children (verified end-to-end).
- `--no-skills` — layer 2. Never pass `--skill` on a delegation. Both layers
  are required; an unbounded recursive Pi subagent swarm has happened.
- `--model` — pass `planned_override` on every turn; Pi has no inherited-model
  path. A rejected model → the extractor does not run → missing status → the
  shared aborted retry with the same override.
- `--mode json` — event JSONL, one object per line: `session`, `agent_start`,
  `turn_start`, then `message_start` / `message_update`* / `message_end` per
  message, then `turn_end`, `agent_end`, `agent_settled`. `pi_reply.js` takes
  the **last** `turn_end` (one per tool round), requires
  `.message.stopReason === "stop"`, concatenates its `{"type":"text"}` blocks
  in order, and drops `thinking`. `.message.usage` carries
  `{input, output, cacheRead, cacheWrite, totalTokens, cost:{...,total}}`.
- `--session-dir <run-dir>/sessions` — keeps the child transcript on disk
  (Execution step 4's promise) and out of the real session store. Never
  `--no-session`.

Cold start is ≈15 s per one-shot `pi -p`; a turn under a minute old is not
stalled.

## P2 — Time-bound (the bash tool's native `timeout`)

Always pass `timeout` (seconds) explicitly on the `bash` call — there is no
default; the maximum is `MAX_TIMEOUT_MS = 2_147_483_647`.

| turn mode | `timeout` (seconds) |
|---|---|
| `survey` | 3600 |
| `plan` | 2400 |
| `execute` | 2400 |
| `debug` | 900 |
| `review-process` | 900 |
| `review-dev` | 1800 |
| anything else | 900 |

- `review-doc` deliberately has no row (a ruling): it takes `anything else`.
  Reopen only if a real review-doc turn times out; then add a row here and in
  `scripts/watchdog.sh` together.
- An inserted decision turn runs as `review-dev` → 1800. A `--decider=human`
  wait makes no call.
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
- SKILL.md Execution step 4's late-reply clause never fires here: P2 *is* the
  `bash` call, so a verdict and a reply are mutually exclusive by construction.
  No grace window.

## `aborted` handling (Execution step 4)

The `bash` call returned the timeout error → `aborted` immediately. Do not read
the child's partial output; it is on disk under `--session-dir`. Record the
session path in the run index.

### Re-run keying

- No keyed watchdog exists; a re-run is the same call, same prompt file, same
  deliverable path. Child sessions are UUID-named, so nothing is overwritten.
- A command the shell rejected is still an attempt: record it
  (`reported_status: null`, `effective_status: "aborted"`, `file: "-"`) and
  retry as the next attempt number. Re-issuing under the same key makes two
  commands name one attempt (`U-COMMAND-ATTEMPT` in the adaptive audit).

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
