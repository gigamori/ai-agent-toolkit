# Harness reference — Claude Code

Read this when Step -1 resolved the harness to Claude Code. It defines the two
harness-specific primitives that `SKILL.md`'s `## Execution` refers to:
**P1 delegation** and **P2 time-bound**.

## P1 — Delegation

Delegate the turn to one general-purpose subagent, requesting the turn's
resolved `model` as the delegation-call model override when the platform
supports it; otherwise proceed with the inherited model and record that in the
run index. One turn = one subagent; never combine turns.

The subagent writes its deliverable to the run directory as `NN-<mode>.md` and
returns only a ≤3-line gist followed by the reply-contract status line (see
`SKILL.md`'s Injection assembly and Execution step 3).

## P2 — Time-bound (the turn watchdog)

**Start the turn watchdog in the same message as the delegation call**, as a
background command:

```
bash <this skill's dir>/scripts/watchdog.sh --deliv <the turn's deliverable path> --desc "<the description given on the delegation call, verbatim>" --mode <the turn's mode>
```

Use `--deliv -` for a turn that writes no file. **Every delegation in the run
needs its own description**, re-runs and recovery turns included — that string
is the watchdog's only key, so two turns sharing one can be confused for each
other.

The watchdog is what bounds the turn in wall-clock time. The orchestrator is
event-driven: it cannot poll, and the only asynchronous wake available to it is
the completion of a background command it started itself. Without the watchdog
a subagent that never returns leaves the run waiting indefinitely, which is the
failure this whole mechanism exists to prevent.

It exits with one word — `DONE`, `TIMEOUT`, or `STALL` — and that exit is the
wake. Thresholds live at the top of the script; do not pass the delegation
call's agent id to it, since it resolves the transcript from `--desc` on its
own.

`DONE` means the deliverable was written *during this turn* — the file must
also be newer than the watchdog's start, not merely present. This matters
because a re-run reuses the deliverable path, so on any second attempt the file
is already there from the first.

Whichever finishes first wins. When the turn's own completion notification
arrives first, stop the watchdog task: a verdict that lands after the turn
already has a readable status is stale and must be ignored, never
re-classified.

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
  partial file the aborted attempt managed to write is ignored rather than
  read as an instant `DONE`. Pass the same `--deliv` you passed the first time.

## Out-of-scope note

No helper scripts beyond the turn watchdog (`scripts/watchdog.sh`, with
`scripts/watchdog_test.sh` covering it) — prompt assembly and the Step 0 gate
are done in-prompt. The watchdog is a script on purpose: it is the one part of
a turn that must behave identically every time, and re-typing it per turn
would put the wall-clock bound back under the same improvisation it exists to
bound. Its tests ship with it for the same reason — a bound nobody re-checks is
a bound that quietly stops holding.

## Residual risks specific to this harness

- The watchdog's own guarantee is bounded: it proves a turn exceeded a budget,
  never that the turn was wrong. Its thresholds are wall-clock guesses, so they
  are calibrated to over-wait rather than cut a slow turn short.
- `STALL` cannot distinguish a hung subagent from one that is thinking or
  sitting in a long tool call — the transcript looks identical in all three.
  The threshold is therefore set well above the longest tool call a turn is
  expected to make, which means a genuine hang is detected late rather than
  not at all. Lowering it trades that delay for turns killed while still
  working.
