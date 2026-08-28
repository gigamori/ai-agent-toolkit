# Harness reference — Pi

Read this when Step -1 resolved the harness to Pi. It defines the two
harness-specific primitives that `SKILL.md`'s `## Execution` refers to:
**P1 delegation** and **P2 time-bound**. Pi has no subagent-delegation tool and
no background command execution — its only built-in tools are
`read | bash | edit | write | grep | find | ls` (`dist/core/tools/index.d.ts`
`ToolName`, checked 2026-07-29). Delegation and the time-bound are therefore
both implemented through the `bash` tool.

## P0 — Paths (resolve once, before the first delegation)

Every delegation needs two absolute paths. They sit under **two different roots,
and the roots are not interchangeable** — the pi CLI is installed by npm, while
the extractor ships with this skill inside the agent dir. Resolve each with its
own command, record both in the run index, and reuse them for every turn.
Step -1's bounded-resolution rule applies: if a command fails, the run is
`blocked` and you never go looking for the file.

**`<pi-cli-entry>`** — the pi CLI. Root = **the npm global prefix**, never the
agent dir.

```
npm prefix -g
```

Join that output with `node_modules/@earendil-works/pi-coding-agent/dist/cli.js`.
Verified 2026-08-17 (pi 0.84.1, win32): the entry reached this way is
byte-identical to the local source build's `dist/cli.js` and answers
`--version`.

**`<pi-reply>`** — the reply extractor, this skill's own `scripts/pi_reply.js`.
Root = **the agent dir**, never the npm prefix.

```
node -e "console.log(require('path').resolve(process.env.PI_CODING_AGENT_DIR || require('os').homedir() + '/.pi/agent', 'skills/mode-orchestrator/scripts/pi_reply.js'))"
```

That output is already the whole path: node resolved it, so it comes back in the
platform's own path form, and it honors `PI_CODING_AGENT_DIR` — the lever the
shadow-agent-dir measurement procedure depends on
(`docs/skills/mode-orchestrator/AUTHORING_CONTRACT.md`). Both properties are lost
the moment you hard-code `~/.pi/…` instead.

Measured 2026-08-17: an orchestrator holding both commands still looked for the
**CLI under the agent dir** first. It recovered cheaply — one `ls`, nothing
there, switch to the npm prefix — which is the bounded behaviour this section
exists to produce rather than a scan. Naming the root on each line is what
removes the swap itself.

**Why this section exists — measured 2026-08-17.** Writing the extractor call
as `node ~/.pi/agent/skills/…/pi_reply.js` breaks *precisely when* the
`MSYS_NO_PATHCONV=1` guard below is in force — which is whenever a driver set
it and this skill's `bash` tool inherited it. The tilde expands to `/c/Users/…`,
the guard stops anything from rewriting it, and node fails with
`Cannot find module 'C:\c\Users\…'`; the doubled root is the signature. Two of
three Pi runs lost that day were an agent responding to a path that would not
resolve by scanning the whole filesystem for the file. The Windows-form rule
stated under P1 was already correct — this section is what makes the two paths
actually obey it.

## P1 — Delegation

Delegate the turn as a blocking, non-interactive `pi -p` call from the `bash`
tool, passing the assembled prompt (Injection assembly's output) as the
**positional message argument**.

Two things that look like the obvious way to do this are wrong; both were
measured on 2026-07-29 against pi v0.80.6 / win32, and the truncation hazard
was **re-measured 2026-08-13 against pi v0.84.1 / win32 (this machine's
current global install, matching pi-studio's local source dependency) —
unchanged, with a control arm.**

- **Do not deliver the prompt with `@<file>`.** The include syntax attaches
  the file as *content to reason about*, not as the turn's instruction. A
  probe whose file said "reply with exactly this JSON object and nothing
  else" came back with a refusal that named it an embedded-instruction
  (prompt-injection) attempt and asked what the user actually wanted. The
  identical text passed positionally was obeyed.
- **Do not invoke `pi.CMD` (or `pi.ps1`) with a multi-line prompt on argv.**
  Its cmd.exe layer **truncates the argument at the first newline** — a
  two-line probe reached the model as line one only, exit code 0, nothing on
  stderr. This is the same corruption class `reliability-spec.md` §13.2
  measured for the claude shim, and the assembled prompt is always
  multi-line. **Which launcher `pi` resolves to depends on the calling
  shell**, not on the OS alone — corrected 2026-08-13, the original wording
  ("on Windows `pi` resolves to `pi.CMD`") was imprecise: a Git Bash / MSYS
  shell resolves the extensionless `pi` to the npm-installed POSIX shim
  (`#!/bin/sh`, body `exec node .../cli.js "$@"`), which passed a two-line
  probe intact; only invoking through cmd.exe or PowerShell reaches
  `pi.CMD`/`pi.ps1` and hits the truncation. The safe instruction is
  unchanged either way — bypass every shell-resolved launcher and go
  straight to the entry — `<pi-cli-entry>`, which P0 resolved. Verified
  2026-08-13: with `pi.CMD` invoked
  directly, a two-line probe ("Say APPLE.\nDisregard the previous line:
  instead reply with exactly one word, BANANA.") returned `APPLE.`
  (truncated) — the node-entry control arm, identical prompt, returned
  `BANANA` (intact).
- **Driving-side hazard for MSYS shells (Git Bash), measured 2026-08-13 with
  a control**: MSYS argument path-conversion rewrites a positional argument
  that *begins* with a POSIX-looking path — a headless invocation prompt
  starting `/mode-orchestrator …` reached the pi process as
  `C:/Program Files/Git/mode-orchestrator …`. Set `MSYS_NO_PATHCONV=1` on the
  launch (with the guard the same prompt arrived intact). Do **not** reach for
  `MSYS2_ARG_CONV_EXCL='*'` instead: it also stops the node entry's own path
  from resolving and the launch fails outright (`Cannot find module
  'C:\c\Users\…'`) — set only `MSYS_NO_PATHCONV=1` and write every path handed
  to node in Windows form. This concerns whoever launches a pi session from an
  MSYS shell (a headless driver, or this orchestrator's own `bash` tool if a
  prompt ever begins with such a token); the assembled delegation prompts
  start with header text, not a slash token, so delegations measured to date
  were unaffected.

Recommended invocation shape. **Assign the prompt to a shell variable on its
own command first and reference it as `"$PROMPT"`** — an inline multi-line
prompt has twice died at bash parse time before launching anything. Both `<pi-cli-entry>` and
`<pi-reply>` are the values P0 already resolved and recorded — substitute them,
do not re-derive them per turn:

```
MODE_ORCH_DEPTH=1 node <pi-cli-entry> -p --mode json \
   --model <the turn's resolved model> \
   --no-skills --session-dir <run-dir>/sessions "$PROMPT" \
   < /dev/null > <run-dir>/raw/<turn-key>-<attempt>.jsonl \
 && node <pi-reply> <run-dir>/raw/<turn-key>-<attempt>.jsonl
```

**You do not read the event stream.** It goes to a file; what you read is the
extractor's stdout, which is the turn's reply text and nothing else. The rest
of this section still documents the stream's shape, because the extractor is
built on it and a version bump can change it — but that is background, not a
thing to parse by hand in a turn.

Why the seam exists, measured 2026-08-13 (pi 0.84.1, the Pi facet E2E): the
`--mode json` stream is orders of magnitude larger than the reply it carries —
a captured orchestrator stream ran **5.7 MB across 18 `turn_end` events and 145
`message_update` deltas**, and even a clean terminal `turn_end` carries a
`thinking` block beside the reply text. Handed to you as a tool result, that
overflows the `bash` tool's result budget, which spills it to a temp log
(`%TEMP%/pi-bash-<hash>.log`) that you then have to read to find the reply.
That is exactly what happened in the E2E, and it drags a whole turn's raw
transcript into the context this skill's Context discipline exists to protect.
The extractor closes that path structurally rather than by asking you to be
careful.

- **`<run-dir>/raw/` must exist before the first delegation** — create it with
  the run directory. The `>` redirect does not create directories, so a
  missing `raw/` fails the very first delegation.
- **When the extractor exits non-zero** it prints one line to stderr and
  nothing to stdout — so the tool result carries no `status:` line, and
  Execution step 4 classifies the turn `aborted` through its missing-status
  path. No extra rule is needed: exit 3 means the stream held no `turn_end`,
  exit 4 means the terminal one did not stop cleanly (`stopReason` and any
  `errorMessage` are named in the stderr line), exit 2 is a usage or read
  error. Its self-tests are `scripts/pi_reply_test.sh`, whose fixtures are
  carved from a real captured stream.
- **The raw stream stays on disk** as `<run-dir>/raw/<turn-key>-<attempt>.jsonl`.
  Append that exact path as the attempt's `delegation_ref`. Its first `session.id`
  joins the attempt to the child transcript whose first session record has that id.

- `< /dev/null`: **determinism, not diagnostics.** A child `pi -p` inherits this
  turn's stdin, and pi reads it unconditionally whenever stdin is not a TTY —
  `readPipedStdin()` (`src/main.ts`, pi 0.84.1) resolves only on stdin's `end`
  event, with no timeout and no fallback, and runs even when the prompt was
  passed on argv. Two failures follow from an inherited stdin, and the second is
  why this is not optional:
  - **An stdin that never closes hangs the child forever, silently.** Not one
    byte reaches stdout — the session header is written later than this wait,
    and startup diagnostics are redirected away by pi's own stdout takeover, so
    stdout and stderr are both empty. There is nothing to read and nothing to
    time out.
  - **An stdin carrying even one byte rewrites the prompt.**
    `buildInitialMessage()` (`src/cli/initial-message.ts`) pushes the stdin
    content *ahead of* the prompt argument and concatenates with
    `parts.join("")` — **no separator**. The turn no longer receives the prompt
    you assembled, and any leading slash token is no longer leading, so it stops
    resolving as a command. This failure **starts normally**, so it never reads
    as a hang: it reads as a turn that ran and ignored its instructions, which
    is far more expensive to diagnose than a stall.

  Scope: **every child `pi -p` this skill launches.** The one exception is a
  measurement arm investigating a startup failure — pinning stdin there destroys
  the only evidence that can attribute it (whether fd 0 was still open when the
  child stalled), so such an arm leaves stdin inherited and records fd 0's state
  instead. Ordinary runs are not measurement arms; pin them.
- `MODE_ORCH_DEPTH=1`: **recursion guard, layer 1.** `SKILL.md` Step -1 reads
  this variable before anything else and stops the run if it is set. Pi's
  `bash` tool passes `process.env` through to children
  (`dist/utils/shell.js`'s `getShellEnv()`, confirmed present in v0.84.1's
  `dist/utils/shell.js:111`), so the marker reaches any orchestrator that
  starts inside this turn — including one reached by a path `--no-skills`
  does not cover. **Propagation confirmed end-to-end 2026-08-13** (v0.84.1):
  a `pi -p` process launched with `MODE_ORCH_DEPTH=1` in its own environment,
  told to run Step -1's exact probe command through its **own** `bash` tool,
  reported `depth:[1]` back — a control run launched identically but without
  the variable reported `depth:[]`. This is the propagation the guard depends
  on, exercised through the same tool-spawn path a nested delegation would
  use, not inferred from reading the source alone.
- `--no-skills`: **recursion guard, layer 2.** The child cannot discover or
  load `mode-orchestrator` (or any other skill) by discovery; only an explicit
  `--skill <path>` on the delegation command could, and the delegation command
  must never pass one. Both layers are required — this is a real risk, not a
  hypothetical one: a prior incident had a Pi-originated subagent swarm from
  unbounded recursive invocation (see `handoff-phase3-onward.md` /
  `handoff-phase5-onward.md` §7, "Pi の nested 呼び出しに再帰ガードがあるか").
- `--model <pattern>`: pass `planned_override` for **every** autonomous turn.
  Pi receives no inherited/default-model path. Before launch write the turn
  definition; after launch append the attempt's exact `actual_override` and raw
  reference. A missing mapping is `blocked`. If Pi rejects a mapped model, the
  `&&` extractor does not run and the missing status follows the shared aborted
  retry using the same override; never fall back across efforts.
- `--mode json`: an **event JSONL stream**, one JSON object per line — not a
  single result object like `claude -p --output-format json`. Measured shape
  (19 lines for a trivial turn): `session`, `agent_start`, `turn_start`, then
  `message_start` / `message_update`* / `message_end` per message, then
  `turn_end`, `agent_end`, `agent_settled`. **This is what `pi_reply.js`
  parses on your behalf; it is documented so the extractor is auditable, not
  so you read it in a turn.** Its rule: take the **last** `turn_end` (one is
  emitted per tool round — 18 in a measured 17-tool-call run), require
  `.message.stopReason === "stop"`, and concatenate that message's
  `{"type":"text"}` blocks in order — the reply whose final line carries the
  reply-contract `status:` line. `thinking` blocks sit in the same array when
  the model reasons and are dropped. `.message.usage` carries
  `{input, output, cacheRead, cacheWrite, totalTokens, cost:{...,total}}` for
  anyone reading the saved stream afterwards.
- **Two harness-only subdirectories of the run directory.** `SKILL.md`'s Run
  directory section is harness-neutral and names neither; both are created
  with the run directory here: `<run-dir>/raw/` for delegation streams
  (`<turn-key>-<attempt>.jsonl`, created before the first delegation) and
  `<run-dir>/sessions/` for child transcripts. The attempt record names the raw
  stream; the raw session id joins it to the child transcript, so both are
  evidence rather than anonymous files.
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
| `survey` | 3600 |
| `plan` | 2400 |
| `execute` | 1500 |
| `debug` | 900 |
| `review` | 900 |
| `review-dev` | 1800 |
| anything else | 900 |

An inserted **decision turn** (`SKILL.md` Execution step 7) runs as
`mode: review-dev` and therefore takes the `review-dev` row (1800 s) — the same
budget the CC watchdog gives it. Nothing in this mechanism is decision-specific.
A `--decider=human` wait makes no `pi -p` call at all, so no `timeout` applies
to it; see below.

These are the CC watchdog's `DEADLINE_*` values verbatim
(`scripts/watchdog.sh`, re-verified 2026-08-20), so the same turn gets the same
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
- There is no separate completion wake either, and none is needed: when the
  `bash` call returns without a timeout error, the turn is already complete. On
  CC the completion notification is what ends the normal path, and the
  orchestrator must stop the watchdog there; here there is no background process
  to stop.
- **SKILL.md Execution step 4's late-reply clause never fires on this harness,
  and needs no grace window here.** That clause covers a P2 verdict firing and
  the turn's reply arriving anyway — a race that requires P2 to run as a process
  separate from the turn. Here P2 *is* the `bash` call: it returns either the
  turn's output or the timeout error, never both, so the two outcomes are
  mutually exclusive by construction and the ordering cannot occur. Stated
  explicitly because the clause's absence would otherwise read as an omission
  rather than as a property of this mechanism. (Judged 2026-08-27, when the
  ordering was observed on the CC facet.)

**Verified end-to-end, 2026-08-13** (pi v0.84.1, the Pi facet E2E — closing
what `handoff-phase5-onward.md` §7 listed as "bash timeout の kill がターン分類に
使えるか", open since 2026-07-29): the timeout error surfaces to the
orchestrator's own turn **verbatim**, neither swallowed nor reworded, at both
scales measured —

- throwaway probe: a `bash` call `{"command": "sleep 120", "timeout": 20}`
  returned `Command timed out after 20 seconds` as the tool result;
- live run: a `survey` delegation that overran its 600 s budget returned
  `Command timed out after 600 seconds`, and the orchestrator classified the
  turn `aborted` and re-ran it exactly once.

The classification-and-re-run loop itself therefore works on this harness.
(One adjacent obligation was observed dropped in the same run — recording the
abort event in the run index — but that is an orchestrator-compliance matter
under SKILL.md's Run directory section, not a property of this mechanism;
tracked with the E2E's defect candidates, `mo-pi-facet-e2e.md` §8.)

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

**A command that dies before a child session exists is not an attempt.** It
launched nothing and wrote nothing, so it takes no `attempt` record and
consumes no retry: re-issue under the **same** `<turn-key>-<attempt>` and note
the lost command in prose above the index's JSONL block. Numbering it as a
second attempt would spend the single identical re-run Execution step 4 allows
on the orchestrator's own quoting mistake, leaving nothing for the abort that
rule exists to cover. Measured twice, 2026-08-28: both times the audit flagged
`U-COMMAND-ATTEMPT` for two commands naming one attempt's raw path, and the
second time that blocked a fixture registration.

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
