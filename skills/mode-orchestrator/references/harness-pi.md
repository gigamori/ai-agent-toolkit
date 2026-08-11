# Harness reference — Pi

Read this when Step -1 resolved the harness to Pi. It defines the two
harness-specific primitives that `SKILL.md`'s `## Execution` refers to:
**P1 delegation** and **P2 time-bound**. Pi has no subagent-delegation tool and
no background command execution — its only built-in tools are
`read | bash | edit | write | grep | find | ls` (`dist/core/tools/index.d.ts`
`ToolName`, checked 2026-07-29). Delegation and the time-bound are therefore
both implemented through the `bash` tool.

## P1 — Delegation

Delegate the turn as a blocking, non-interactive `pi -p` call from the `bash`
tool, passing the assembled prompt (Injection assembly's output) as the
**positional message argument**.

Two things that look like the obvious way to do this are wrong; both were
measured on 2026-07-29 against pi v0.80.6 / win32.

- **Do not deliver the prompt with `@<file>`.** The include syntax attaches
  the file as *content to reason about*, not as the turn's instruction. A
  probe whose file said "reply with exactly this JSON object and nothing
  else" came back with a refusal that named it an embedded-instruction
  (prompt-injection) attempt and asked what the user actually wanted. The
  identical text passed positionally was obeyed.
- **Do not invoke the npm `pi` shim with a multi-line prompt on argv.** On
  Windows `pi` resolves to `pi.CMD`, and its cmd.exe layer **truncates the
  argument at the first newline** — a two-line probe reached the model as
  line one only, exit code 0, nothing on stderr. This is the same corruption
  class `reliability-spec.md` §13.2 measured for the claude shim, and the
  assembled prompt is always multi-line. The shim's own body is just
  `node <entry> %*`, so launch that entry through node directly:
  `node <npm-prefix>/node_modules/@earendil-works/pi-coding-agent/dist/cli.js`.
  Verified: with the shim bypassed, a two-line prompt containing `&` and `|`
  arrived intact.

Recommended invocation shape (write it to a shell variable first so the
multi-line prompt is quoted exactly once):

```
MODE_ORCH_DEPTH=1 node <pi-cli-entry> -p --mode json \
   --model <the turn's resolved model> \
   --no-skills --session-dir <run-dir>/sessions "$PROMPT"
```

- `MODE_ORCH_DEPTH=1`: **recursion guard, layer 1.** `SKILL.md` Step -1 reads
  this variable before anything else and stops the run if it is set. Pi's
  `bash` tool passes `process.env` through to children
  (`dist/utils/shell.js`'s `getShellEnv()`), so the marker reaches any
  orchestrator that starts inside this turn — including one reached by a path
  `--no-skills` does not cover.
- `--no-skills`: **recursion guard, layer 2.** The child cannot discover or
  load `mode-orchestrator` (or any other skill) by discovery; only an explicit
  `--skill <path>` on the delegation command could, and the delegation command
  must never pass one. Both layers are required — this is a real risk, not a
  hypothetical one: a prior incident had a Pi-originated subagent swarm from
  unbounded recursive invocation (see `handoff-phase3-onward.md` /
  `handoff-phase5-onward.md` §7, "Pi の nested 呼び出しに再帰ガードがあるか").
- `--model <pattern>`: the turn's resolved model override (`pi --help`), for
  tiers 1 and 2. **Tier 3 (`inherit`) has no resolved model, and Pi has no way
  to inherit yours** — measured 2026-08-12: a `pi -p --mode json` call with no
  `--model` reported `"model":"gpt-5.4-mini"`, this machine's configured pi
  default, while the identical call with `--model haiku` reported
  `"model":"haiku"`. The control arm is what makes the first reading mean
  something: the field does track the override, so its value with the flag
  omitted really is pi's own default and not an echo of the caller.
  So on this harness: omit `--model` for a tier-3 turn and record the override
  as `none` — but understand that the turn then runs on **pi's configured
  default, which need not be a Claude model at all**. The mode fragments and
  the reply contract were written and measured against Claude models; a run
  that leaves its models to tier 3 here is running them somewhere else. If a
  run needs a particular model, do not lean on tier 3 — pin it on the step or
  supply a spec `mode→model` table. (Passing the orchestrator's own model
  instead would restore true inheritance, but nothing measured says a pi
  session can read its own model, so that is not specified here.)
  Canonical names resolve correctly on Pi as-is — `haiku` →
  `pi-claude-agent-sdk/haiku`, `sonnet` → `pi-claude-agent-sdk/sonnet`,
  `opus` → `pi-claude-agent-sdk/claude-opus-4-8[1m]` — provided the
  `pi-claude-agent-sdk` provider is loaded. Do not pass `--no-extensions`: it
  unloads that provider and every canonical name then fails to resolve.
- `--mode json`: an **event JSONL stream**, one JSON object per line — not a
  single result object like `claude -p --output-format json`. Measured shape
  (19 lines for a trivial turn): `session`, `agent_start`, `turn_start`, then
  `message_start` / `message_update`* / `message_end` per message, then
  `turn_end`, `agent_end`, `agent_settled`. **Read the turn's reply from the
  `turn_end` line**: `.message.content` is a block array (`{"type":"text",
  "text":...}`, plus `thinking` blocks when the model reasons) and
  `.message.stopReason` is `"stop"` on a clean finish. Concatenate the `text`
  blocks in order to get the reply whose final line must carry the
  reply-contract `status:` line. `.message.usage` carries
  `{input, output, cacheRead, cacheWrite, totalTokens, cost:{...,total}}`.
- `--session-dir <run-dir>/sessions`: keeps the child's transcript **on disk**
  — `SKILL.md` Execution step 4 tells the orchestrator to discard an aborted
  turn's output and leave "the transcript on disk for a human to read", and
  that promise has to be kept here — while keeping these one-shot sessions out
  of the real session store. **Do not use `--no-session`**: it satisfies
  neither half.

**Cold-start cost.** A one-shot `pi -p` run is not instant: five consecutive
cold judgment calls (`haiku`, trivial question) took 16.2–16.5 s each, 5/5
successful. Treat ~15 s as the floor of any turn's wall-clock, and do not read
a turn that has been running for well under a minute as stalled. The
flakiness pitfall recorded for cold `pi -p` in
`_projects/pi-extensions-dev/rules.md` did **not** reproduce for this call
shape (no in-process child session is spawned here); it remains relevant for
delegations that load extensions which start their own child sessions.

## P2 — Time-bound (the bash tool's native `timeout`)

Pass a `timeout` (seconds) on the `bash` tool call that runs `pi -p`. The
`bash` tool's schema takes `{ command: string, timeout?: number }`
(`dist/core/tools/bash.d.ts` `BashToolInput`) with **no default** — always
pass one explicitly, taken from this table. The upper bound is
`MAX_TIMEOUT_MS = 2_147_483_647` ms (~24.8 days;
`dist/core/tools/bash.js`).

| turn mode | `timeout` (seconds) |
|---|---|
| `survey` | 600 |
| `plan` | 2400 |
| `execute` | 1500 |
| `debug` | 900 |
| `review` | 900 |
| `review-dev` | 900 |
| anything else | 900 |

An inserted **decision turn** (`SKILL.md` Execution step 7) runs as
`mode: review-dev` and therefore takes the `review-dev` row (900 s) — the same
budget the CC watchdog gives it. Nothing in this mechanism is decision-specific.
A `--decider=human` wait makes no `pi -p` call at all, so no `timeout` applies
to it; see below.

These are the CC watchdog's `DEADLINE_*` values verbatim
(`scripts/watchdog.sh`, read 2026-07-29), so the same turn gets the same
wall-clock budget on either harness. They are **numbers, not a starting
point to re-derive per run**: the whole reason CC's bound lives in a script is
that a wall-clock bound decided fresh each turn is no bound at all, and the
same applies here.

**Do not tighten them below the CC values.** CC's watchdog only reports, and
the orchestrator then stops the turn; Pi's `timeout` kills the process tree
outright. A premature kill destroys work that a report would merely have
flagged, and these values are already calibrated to over-wait rather than cut
a slow turn short (see `harness-cc.md`'s residual risks). Note also that CC
splits its budget in two — a `DEADLINE_*` plus a separate `STALL=600`
inactivity threshold — while Pi has only this single deadline, so here it is
carrying both jobs.

On exceeding the timeout, `dist/core/tools/bash.js`'s `resolveTimeoutMs()` path
calls `killProcessTree(child.pid)` — killing the whole process tree, not just
the top process — and the tool result becomes the string
`Command timed out after <N> seconds` (line ~314 as read 2026-07-29; re-grep
the symbol before depending on the line number, since it drifts).

This single mechanism covers what CC's watchdog splits into `TIMEOUT` and
`STALL`:

- There is no `STALL` equivalent, and none is needed: the orchestrator's own
  turn is blocked on the `bash` call, so it is not polling a transcript in the
  first place. If the child `pi -p` hangs, the timeout above ends it either
  way.
- There is no `DONE` equivalent, and none is needed: when the `bash` call
  returns without a timeout error, the turn is already complete — there is no
  separate wake to wait for.

**Unverified (flag before relying on in a live run):** whether the timeout
error surfaces to the orchestrator's own turn in a form it can reliably
classify as `aborted` (as opposed to, e.g., being swallowed or reworded
somewhere in the calling chain) has not been confirmed end-to-end. This is
listed as an open item in `handoff-phase5-onward.md` §7 ("bash timeout の kill
がターン分類に使えるか"). Confirm it with a deliberately-hanging throwaway
command before the first real Phase 5 E2E run.

## `aborted` handling (Execution step 4)

**The `bash` call returns the timeout error.** Classify the turn `aborted`
immediately — there is no separate wake to wait for, since the orchestrator's
own turn was blocked on the call. Do not try to read the child pi process's
partial output first: it is on disk as its own session under the
`--session-dir` passed above, and pulling it into the orchestrator's context
to describe a turn that is being discarded anyway can end the run outright.
Record the session path in the run index so a human can find it.

### Re-run keying

There is no keyed watchdog process to confuse a re-run with — the `bash` call
that timed out is already gone. Re-run means: issue the same `pi -p` call
again, same prompt file, same output path for the turn's deliverable. No
distinct description is needed, since nothing keys off one. The child's own
session file is named by a pi-generated UUID, so a re-run does not overwrite
its aborted predecessor's transcript.

## `--decider=human` waiting

The wait works here, and needs nothing built. Pi's orchestrator blocks on the
`bash` tool only for the duration of a delegation call (P1 above); between
steps it is an ordinary agent turn. So ending the turn at a step boundary to
present a `## Decision request` behaves exactly as it does on Claude Code — in
an interactive `pi` session the user answers and the run continues; under
`pi -p` nothing can answer and the run ends, which is the intended degradation.

Note that this concerns the orchestrator's **own** session, whichever way it was
started. It is unrelated to the `-p` on the delegation command line above: every
delegated turn is headless on this harness by construction, and none of them
ever waits for a human.

## Denial check

None exists: pi has no permission layer at all (its CLI and core were grepped
for a permission concept — zero hits), so a tool call cannot be "denied by the
permission system" and the reply contract's denial clause can never trigger on
this harness. There is no Pi counterpart to `scripts/deny_scan.sh`.

**What this means for `blocked`.** `SKILL.md` Execution step 3 gives `blocked`
two sources — machine-detected denial, and the turn judging for itself that it
reached an irreversible or outward-facing effect no rule permits. Source (1)
does not exist here, so on Pi the entire weight sits on source (2): a sentence
in the injected prompt, obeyed or not by the subagent. The "unless a rule
permits it" half is in the same position — on Claude Code the settings
allowlist decides mechanically, here the subagent decides by reading. Do not
carry over an intuition formed on Claude Code, where the permission layer stops
most such calls before any prompt rule matters.

## Residual risks specific to this harness

- The bash tool's `timeout` bounds wall-clock time exactly, with no separate
  detection for "still generating but past a reasonable pace" — a turn that is
  legitimately still working when the timeout fires is killed exactly as hard
  as a genuinely hung one. Size the timeout generously per mode to compensate;
  there is no cheaper middle ground available the way CC's `STALL` threshold
  provides one.
- Recursion protection is two layers (`MODE_ORCH_DEPTH=1` checked by Step -1,
  plus `--no-skills`), but both live on the same delegation command line — a
  delegation command written without them defeats the whole guard at once.
  Neither layer is enforced by anything outside that command string.
