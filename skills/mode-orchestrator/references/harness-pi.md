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
tool. Write the assembled prompt (Injection assembly's output) to a file in
the run directory first, then pass it with the `@<path>` include syntax rather
than as an inline argument — `pi @<prompt-file> -p` includes the file's
content as the initial message (confirmed via `pi --help`'s "Include files in
initial message" example). This avoids shell-quoting a long, multi-section
prompt.

Recommended invocation shape:

```
MODE_ORCH_DEPTH=1 pi @<prompt-file> -p --mode json --model <the turn's resolved model> \
   --no-skills --session-dir <run-dir>/sessions
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
- `--model <pattern>`: the turn's resolved model override (`pi --help`).
  Canonical names resolve correctly on Pi as-is — `haiku` →
  `pi-claude-agent-sdk/haiku`, `sonnet` → `pi-claude-agent-sdk/sonnet`,
  `opus` → `pi-claude-agent-sdk/claude-opus-4-8[1m]` — provided the
  `pi-claude-agent-sdk` provider is loaded. Do not pass `--no-extensions`: it
  unloads that provider and every canonical name then fails to resolve.
- `--mode json`: structured output mode, easier to extract the reply-contract
  status line from than the default text mode.
- `--session-dir <run-dir>/sessions`: keeps the child's transcript **on disk**
  — `SKILL.md` Execution step 4 tells the orchestrator to discard an aborted
  turn's output and leave "the transcript on disk for a human to read", and
  that promise has to be kept here — while keeping these one-shot sessions out
  of the real session store. **Do not use `--no-session`**: it satisfies
  neither half.

**Unverified (flag before relying on in a live run).** Neither of these has
been confirmed against a real run in this project; confirm both once, on a
throwaway prompt, before the first real Phase 5 E2E run, and do not assume
either.

1. The exact shape of `--mode json`'s stdout — specifically where the model's
   final assistant text sits in the emitted JSON. Do not assume a schema.
2. Whether `pi @<prompt-file> -p` processes the included file as the turn's
   message with **no additional message argument**. `pi --help`'s worked
   example for `@file` pairs it with a message string
   (`pi @prompt.md @image.png "What color is the sky?"`), so the
   file-only form is an extrapolation. If it turns out a message argument is
   required, pass a short fixed one (the prompt file stays the real payload).

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
