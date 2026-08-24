# Harness reference — Claude Code

Read this when Step -1 resolved the harness to Claude Code. It defines the two
harness-specific primitives that `SKILL.md`'s `## Execution` refers to:
**P1 delegation** and **P2 time-bound**.

## P1 — Delegation

Delegate the turn to one general-purpose subagent, passing the resolved profile
or explicit-step `model` as the delegation-call override. Record its effort,
model source, resolved model, and the override actually passed. One turn = one
subagent; never combine turns.

Every autonomous turn has an override. A profile setup fault is `blocked`
before delegation; a launch rejection yields no valid status and follows the
shared `aborted` handling. Never fall back across efforts or to the session
model.

The subagent writes its deliverable to the run directory as `NN-<mode>.md` and
returns only a ≤3-line gist followed by the reply-contract status line (see
`SKILL.md`'s Injection assembly and Execution step 3).

## P2 — Time-bound (the turn watchdog)

**Start the turn watchdog in the same message as the delegation call**, as a
background command:

```
bash <this skill's dir>/scripts/watchdog.sh --deliv <the turn's deliverable path> --desc "<the description given on the delegation call, verbatim>" --mode <the turn's mode>
```

`<this skill's dir>` is the directory this reference file was read from, so
Step -1 already put it in hand — there is nothing to derive and no Claude Code
counterpart to the Pi facet's P0 recipe (its CLI entry has no analogue here,
since delegation goes through the subagent tool rather than a command). If the
path is somehow not at hand, Step -1's bounded-resolution rule decides it: the
run is `blocked`; never scan the filesystem for the script.

Use `--deliv -` for a turn that writes no file. **Every delegation in the run
needs its own description**, re-runs and inserted turns of either loop included
— that string is the watchdog's only key, so two turns sharing one can be
confused for each other.

An inserted **decision turn** (`SKILL.md` Execution step 7) is an ordinary
delegated turn here and needs nothing new: it runs as `mode: review-dev`, so
`--mode review-dev` gives it `DEADLINE_REVIEW_DEV` (1800 s, `scripts/watchdog.sh`
— unchanged; whether a decision turn deserves its own budget is still open),
and `--deliv` takes its decision record (`NNa-decision.md` in the simple
case; the actual name is the next free suffix letter). A `--decider=human`
wait involves no
delegation at all, so there is no watchdog to start and no description to key:
the orchestrator simply ends its turn at the step boundary.

The watchdog is what bounds the turn in wall-clock time. The orchestrator is
event-driven: it cannot poll, and the only asynchronous wake available to it is
the completion of a background command it started itself. Without the watchdog
a subagent that never returns leaves the run waiting indefinitely, which is the
failure this whole mechanism exists to prevent.

It exits with one word — `TIMEOUT` or `STALL` — and that exit is the wake. Both
are trouble verdicts: the watchdog wakes you only when the turn is over its
wall-clock budget or its subagent stopped generating. Thresholds live at the top
of the script; do not pass the delegation call's agent id to it, since it
resolves the transcript from `--desc` on its own.

**Writing the deliverable is not a wake.** The watchdog watches the file and
keeps monitoring after it appears: both verdict messages state whether the turn
had written it, and a fresh write is recorded in the `--log` trace. Exiting there
instead would wake you *before* the turn's own completion notification, with no
defined meaning, and would leave the reply-composing tail of the turn — which
comes after the file is written — with no time bound at all. "Written during
this turn" still governs what the file check counts: the file must be newer than
the watchdog's start, not merely present, because a re-run reuses the deliverable
path, so on any second attempt the file is already there from the first.

**The normal-path wake is the turn's own completion notification, and at that
point you stop the watchdog task** — that is now the only way a well-behaved
turn's watchdog ends. A verdict that lands after the turn already has a readable
status is stale and must be ignored, never re-classified.

## `aborted` handling (Execution step 4)

**The watchdog wakes first with `TIMEOUT` or `STALL`.** The turn is over its
wall-clock budget, or its subagent stopped generating. Stop the turn and
classify it `aborted`. Do not wait for a reply that the watchdog has already
established is not coming. Do not try to read the stopped turn's output first:
a subagent's output file is its entire transcript, and pulling that into the
orchestrator's context to describe a turn that is being discarded anyway can
end the run outright.

### Re-run keying

- The prompt is identical, but **the re-run's description must not be** — give
  it a distinct one (e.g. suffix `(re-run)`). The description is the
  watchdog's only key, and the aborted turn's own record is still on disk and
  still recent; reuse the string and the re-run's watchdog can latch onto its
  dead predecessor, whose transcript by definition stopped growing, and report
  `STALL` immediately against a turn that is working fine.
- The deliverable path, by contrast, stays the same on a re-run, and that is
  fine: the watchdog requires the file to be newer than its own start, so a
  partial file the aborted attempt managed to write is noted once in the trace
  as stale and never counted as this turn's output. Pass the same `--deliv` you
  passed the first time.

## Denial check (after each turn's status line)

After reading a turn's status line, run once:

```
bash <this skill's dir>/scripts/deny_scan.sh --desc "<the delegation description, verbatim>"
```

It resolves the turn's transcript the same way the watchdog does (the
description is the key) and greps it for permission-denial tool results
(`"is_error":true` plus the measured denial phrases). Verdicts:

- `CLEAN` (exit 0) — keep the turn's self-reported status.
- `DENIED <n>` (exit 1) — the turn's status is `blocked` regardless of what
  its reply said (`ok`, `failed`, and `needs-decision` alike); record in the
  run index that the denial was machine-detected. This is the backstop for a
  turn that judged its own denial inessential and reported `ok` — a measured
  incident, not a hypothetical. It is also what stops a permission wall from
  entering the decision loop dressed as a choice.
- `NO-TRANSCRIPT` (exit 2) — the check could not run; say so in the run
  index. Do not treat it as `CLEAN`.

Unlike the watchdog this is not a race: the transcript is complete when the
status line has arrived, so the scan runs synchronously after it.

**Known fragility — serialization drift.** The scan matches the transcript's
literal serialization (`"is_error":true`, no space) and the measured denial
phrases. If a Claude Code update changes either, the scan stops detecting —
silently, in the fail-open direction (the reply-contract clause remains as
the only layer). `deny_scan_test.sh` runs on fixed fake transcripts, so it
cannot notice such drift. Canary: after any Claude Code upgrade — or
whenever a run's turn is *known* to have been denied — run the scan against
a transcript that contains a real denial and confirm it still reports
`DENIED`; a `CLEAN` there means the serialization moved and the patterns at
the top of `deny_scan.sh` need re-measuring.

**This canary has fired once, on 2026-08-12.** Two real Bash denials scanned
`CLEAN` while `"is_error":true` still matched — only the phrasing had moved,
to a `Permission to use <Tool> ... has been denied` template that neither
pattern covered. The scan had been inert for an unknown stretch before
anyone checked, and its test suite stayed green the whole time. Treat the
canary as a real obligation, not a formality: it is the only thing that can
catch this class of failure, and running it costs one command.

### Running this skill itself under `claude -p`

The permission layer applies to the orchestrator too, and the first thing it
hits is `SKILL.md` Step -1's harness probe. Measured 2026-08-12: in a child
`claude -p` with no allow rule for it, the probe is refused even when sent
literally — the classifier returns `Contains expansion`, because `${VAR:-}`
leaves nothing an allow prefix can match. A control run differing only in
carrying a literal allow rule executed the same line and produced the probe
output, so the difference is the rule, not the command text.

Grant that one command before starting a headless run — either a narrow
literal rule in the settings that run reads:

```
"Bash(echo \"harness-probe:[${CLAUDE_CODE_SESSION_ID:-}] depth:[${MODE_ORCH_DEPTH:-}]\")"
```

or the same string passed as `--allowedTools` on the invocation. Without it
the run stops at Step -1 — which is Step -1 working as designed, not a
defect to route around.

## Out-of-scope note

No helper scripts beyond the turn watchdog (`scripts/watchdog.sh`, with
`scripts/watchdog_test.sh` covering it) and the denial check
(`scripts/deny_scan.sh`, with `scripts/deny_scan_test.sh`) — prompt assembly
and the Step 0 gate are done in-prompt. Both are scripts on purpose: they are
the parts of a turn that must behave identically every time, and re-typing
them per turn would put the wall-clock bound and the denial evidence back
under the same improvisation they exist to bound. Their tests ship with them
for the same reason — a bound nobody re-checks is a bound that quietly stops
holding.

## Residual risks specific to this harness

- The watchdog's own guarantee is bounded: it proves a turn exceeded a budget,
  never that the turn was wrong. Its thresholds are wall-clock guesses, so they
  are calibrated to over-wait rather than cut a slow turn short.
- Since the watchdog no longer ends itself when the deliverable appears, a turn
  whose watchdog is not stopped within the stall threshold of the child
  completing produces a stale `STALL` wake, where the old deliverable-triggered
  early exit produced none. The stale-verdict rule above discards it, so this is
  noise rather than a defect — recorded here because an unrecorded new failure
  shape reads as a real one.
- `STALL` cannot distinguish a hung subagent from one that is thinking or
  sitting in a long tool call — the transcript looks identical in all three.
  The threshold is therefore set well above the longest tool call a turn is
  expected to make, which means a genuine hang is detected late rather than
  not at all. Lowering it trades that delay for turns killed while still
  working.
