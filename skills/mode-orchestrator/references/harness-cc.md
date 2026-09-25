# Harness reference — Claude Code

Read this when Step -1 resolved the harness to Claude Code. It defines the two
primitives `SKILL.md`'s `## Execution` refers to — **P1 delegation** and
**P2 time-bound** — plus the denial check.

## P1 — Delegation

- One turn = one general-purpose subagent call, passing `planned_override` as
  the model. The `turn` record is in `index.md` before the call; append an
  `attempt` record after it (actual override, delegation reference/transcript,
  reported/effective status, file). A planned value is never evidence that the
  call happened.
- Every autonomous turn has an override. Profile fault → `blocked` before
  delegation. Launch rejection → no valid status → the shared aborted retry
  with the same effort/source/model/override. Never fall back across efforts
  or to the session model.
- The subagent writes `NN-<mode>.md` in the run directory and returns a
  ≤3-line gist plus the reply-contract status line.

## P2 — Time-bound (the turn watchdog)

Start it in the same message as the delegation call, as a background command:

```
exec bash <this skill's dir>/scripts/watchdog.sh --deliv <the turn's deliverable path> --desc "<the description given on the delegation call, verbatim>" --mode <the turn's mode>
```

- `exec` is load-bearing. Without it, stopping the task kills only the wrapper
  shell; the watchdog keeps polling and fires a stale verdict into a finished
  run. `watchdog_test.sh` pins it.
- `<this skill's dir>` is where this file was read from. Not at hand →
  `blocked`; never scan for the script.
- `--deliv -` for a turn that writes no file.
- Every delegation gets its own `--desc`, re-runs and inserted turns included.
  The description is the watchdog's only key.
- An inserted decision turn is an ordinary turn: `--mode review-dev`
  (`DEADLINE_REVIEW_DEV`), `--deliv` = its decision record. A
  `--decider=human` wait delegates nothing, so no watchdog.
- Thresholds live at the top of `scripts/watchdog.sh`. Do not pass the agent
  id; the script resolves the transcript from `--desc`, within this session
  (it reads `CLAUDE_CODE_SESSION_ID` from the Bash environment).

Wakes:

- Exit word `TIMEOUT` or `STALL` — a trouble verdict, and the only
  asynchronous wake the orchestrator has.
- Writing the deliverable is not a wake. The file counts only if newer than
  the watchdog's start (a re-run reuses the path).
- Normal path: the turn's own completion notification. Stop the watchdog task
  there. A verdict landing after a readable status is stale — ignore it, never
  re-classify.
- Start is unconditional. If the call returns synchronously with no subagent
  (for example an out-of-enum `model` refused at the tool boundary), stop the
  watchdog on reading that error; the re-run starts its own.

Late reply (SKILL.md Execution step 4):

- **Grace window: 120 s past the deadline.** A well-formed `status:` line
  already in hand within it classifies the turn; record the verdict as
  superseded. Not permission to wait.
- Past the window, a reply classifies the turn only on independently verified
  same-turn provenance, both halves required: (1) the subagent still live at
  reply time (`ListAgents`); (2) the end state verified against the run — the
  deliverable newer than this attempt's delegation, and the planned check
  re-run. Never the report's own account of itself. One half or neither →
  `aborted`. Record which path applied.
- `DEADLINE_EXECUTE=2400` is a maintainer ruling, not a measurement. The only
  data are two near-misses under the earlier 1500 s budget (1393 s, 1327 s);
  the tuning question stays open until several build-bearing `execute` turns
  are measured under one budget.

## `aborted` handling (Execution step 4)

Watchdog wakes first → stop the turn, classify `aborted`. Do not wait for the
reply. Do not read the stopped turn's output: its output file is the whole
transcript, and pulling it into the orchestrator's context can end the run.

### Re-run keying

- Same prompt, **distinct description** (e.g. suffix `(re-run)`). The dead
  attempt's record is still recent; a reused key latches the new watchdog onto
  a transcript that stopped growing → immediate false `STALL`.
- Same `--deliv`. The newer-than-start rule discards the aborted attempt's
  partial file.

## Denial check (after each turn's status line)

```
bash <this skill's dir>/scripts/deny_scan.sh --desc "<the delegation description, verbatim>" --out "<run dir>/<turn key>-deny.txt"
```

- `CLEAN` (exit 0) — keep the self-reported status.
- `DENIED <n>` (exit 1) — status is `blocked` regardless of the reply (`ok`,
  `failed`, `needs-decision` alike); record in the run index that the denial
  was machine-detected.
- `NO-TRANSCRIPT` (exit 2) — the check could not run (no transcript resolved,
  or an empty one); say so in the run index. Not `CLEAN`.

Every refusal counts, whatever issued it: a permission rule, a PreToolUse
hook, the auto-mode classifier or its being unreachable, a permission prompt
that was not approved, an interrupted call. The stderr line names the kinds
found. The scan covers the turn's transcript and those of every agent the turn
started, at any depth — through the Agent tool or a Skill that forks one.

Sending and recording it:

- `<turn key>` is the prefix of the turn's deliverable (`05`, `05a`). The
  file's first line is the verdict; the lines after it name the description
  and the transcript scanned.
- Send the command as the only tool call in its message — after the
  watchdog's stop, never beside it. This is mitigation, not a guarantee: in
  one session, all 13 checks whose result never reached the session log had
  shared their message with another call (11 with the watchdog's stop), while
  all 17 sent alone kept theirs.
- Record in the run index the verdict line and the file's path. A verdict in
  neither the command's output nor the file is not recordable: run the check
  again — it only reads the transcript and rewrites its file.
- The command itself refused, or failing before it prints a verdict: record
  the turn as `NO-TRANSCRIPT`. A check that could not run is never `CLEAN`.
- `deny_scan: could not write --out …` on stderr: the verdict on stdout
  stands; record it with that line in place of a path.

Synchronous, not a race: the transcript is complete once the status line has
arrived.

**Serialization drift and the canary.** The scan matches the literal
`"is_error":true` together with the `"toolDenialKind"` field — and, for
transcripts older than that field, the measured denial phrases; a Claude Code
update can make it inert silently (fail-open), and `deny_scan_test.sh` runs on
fixed fakes, so it cannot notice. After any Claude Code upgrade — or whenever a
turn is known to have been denied — scan a transcript containing a real denial
and confirm `DENIED` (for a transcript of another session, put
`CLAUDE_CODE_SESSION_ID=` in front of the command: the scan otherwise searches
only the calling session). A `CLEAN` there means the field or the phrases at the top
of `deny_scan.sh` moved and need re-measuring. This has fired twice (the
phrasing moved to `Permission to use <Tool> ... has been denied`; a PreToolUse
hook refused with its own wording); treat it as an obligation.

### Running this skill itself under `claude -p`

The Step -1 probe is refused in a headless child unless a literal allow rule
exists — the classifier returns `Contains expansion`, because `${VAR:-}` leaves
nothing a prefix can match. Grant this one rule in the settings that run reads,
or pass it as `--allowedTools`:

```
"Bash(echo \"harness-probe:[${CLAUDE_CODE_SESSION_ID:-}] depth:[${MODE_ORCH_DEPTH:-}]\")"
```

Without it the run stops at Step -1, by design.

## Out-of-scope note

Only two helper scripts: `scripts/watchdog.sh` (+ `watchdog_test.sh`) and
`scripts/deny_scan.sh` (+ `deny_scan_test.sh`). They are scripts because the
wall-clock bound and the denial evidence must behave identically every turn;
prompt assembly and the Step 0 gate stay in-prompt.

## Residual risks specific to this harness

- The watchdog proves a turn exceeded a budget, never that it was wrong;
  thresholds are calibrated to over-wait.
- A watchdog not stopped within the stall threshold of the child completing
  emits a stale `STALL`; the stale-verdict rule discards it — noise, not a
  defect.
- `STALL` cannot tell a hang from thinking or a long tool call. The threshold
  sits above the longest expected tool call, so a real hang is detected late
  rather than never.
