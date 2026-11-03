# Harness reference — Claude Code

Read this when Step -1 resolved the harness to Claude Code. It defines the two
primitives `SKILL.md`'s `## Execution` refers to — **P1 delegation** and
**P2 time-bound** — plus the denial check and the moves of the restart gate.

## Delegation permission (before the first delegation)

The permission state comes from the observation route Claude Code publishes —
never from launching a real agent to discover it, and never from scanning
settings files (the skill does not read private configuration). What is
verifiable today: the permission layer enforces at the tool boundary, so a
refusal surfaces after a call is attempted, and the Step -1 probe's own
refusal is policy evidence about that one command. Claude Code publishes no
pre-delegation read of its effective delegation policy, so under the contract
the state reads `unknown` until the host contract lands: the run stops before
delegating, naming the missing publication. A denial observed after a call is
the launch boundary's `blocked` path (the contract's host-result table) —
never grounds to retry the same work in another shape.

## P1 — Delegation

- One turn = one general-purpose subagent call, run in the background, passing
  the approved selection's binding as the call's `model` — the exact concrete
  value the plan approved, at the granularity Claude Code publishes (a model
  reference name, never a tier, never a claimed physical id). The launch
  entry re-checks the permission state and the approved resolution before the
  first model request; the Delegation contract's host-result table classifies
  what happened before any subagent started. The `turn` record is in
  `index.md` before the call; after it, append the `delegation` record (the
  exact selection arguments the call carried, the reference/transcript, the
  reported model/thinking) and the `attempt` record (reported/effective
  status, file). A planned value is never evidence that the call happened.
- Every autonomous turn has an approved binding. Profile fault → `blocked`
  before delegation. A pre-call refusal the permission system issued is the
  launch boundary's `blocked` path: no delegation record, no re-run. A launch
  rejection at the tool boundary that no permission rule caused (for example
  an out-of-enum `model`) started nothing → Execution step 4's direct re-run
  with the same approved binding. Never fall back across efforts or to the
  session model.
- The subagent writes `NN-<mode>.md` in the run directory and returns a
  ≤3-line gist plus the reply-contract status line.
- `<S>`, the attempt's stem, is `<run dir>/<turn key>-<attempt>` (`05-1`,
  `05a-2`). It names the attempt's own files — its denial check, its stop and
  judge records — so no attempt's files are overwritten by another attempt's.

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
- Every delegation gets its own `--desc`, re-runs, inserted turns and judges
  included. The description is the watchdog's only key.
- An inserted decision turn is an ordinary turn: `--mode review-dev`
  (`DEADLINE_REVIEW_DEV`), `--deliv` = its decision record. A
  `--decider=human` wait delegates nothing, so no watchdog.
- A re-run adds `--deadline <the seconds on its ALLOW line>` (Re-run keying).
- Thresholds live at the top of `scripts/watchdog.sh`. Do not pass the agent
  id; the script resolves the transcript from `--desc`, within this session
  when `CLAUDE_CODE_SESSION_ID` is set in the Bash environment (with none, it
  searches every session's logs).
- `DEADLINE_EXECUTE=4800` is a maintainer ruling, not a measurement: every
  `DEADLINE_*` was doubled uniformly by user ruling on 2026-10-04. The only
  data are two near-misses under the earlier 1500 s budget (1393 s, 1327 s);
  the tuning question stays open until several build-bearing `execute` turns
  are measured under one budget.

Wakes:

- Exit word `TIMEOUT` or `STALL` — a trouble verdict, and the only
  asynchronous wake the orchestrator has. It is a notice, not a stop: the
  turn may still run. Go on with **Stop and restart** below.
- Writing the deliverable is not a wake. It is reported as written this turn
  only if the file is non-empty and newer than the watchdog's start.
- Normal path: the turn's own completion notification. Stop the watchdog task
  there. A verdict landing after a readable status is stale — ignore it, never
  re-classify.
- Start is unconditional. If the call returns synchronously with no subagent
  (for example an out-of-enum `model` refused at the tool boundary), stop the
  watchdog on reading that error; the re-run starts its own.
- **A watchdog that does not start leaves the turn unbounded.** If its command
  is refused (a permission rule, a prompt not approved, or a headless
  `claude -p` refusing `exec` even under an exact allow rule) or it exits at
  once, TaskStop the turn's Agent task right away, record the refused command,
  its error and the TaskStop result in the run index prose, append the turn's
  `attempt` record with effective status `blocked`, and stop the run
  (Execution step 8), naming the command. Never start the watchdog without
  `exec` to get past a refusal: the shell then survives TaskStop and keeps
  polling. The same holds for a judge's watchdog (Stop and restart, move 6):
  TaskStop the judge, then stop the run `blocked` the same way.

## Denial check (after each turn's status line; Stop and restart, move 5)

```
bash <this skill's dir>/scripts/deny_scan.sh --desc "<the delegation description, verbatim>" --out "<S>-deny.txt"
```

- `CLEAN` (exit 0) — keep the self-reported status.
- `DENIED <n>` (exit 1) — status is `blocked` regardless of the reply (`ok`,
  `failed`, `needs-decision` alike); record in the run index that the denial
  was machine-detected. On an attempt you called TaskStop for, read it as
  **A stop is not a denial** below says instead.
- `NO-TRANSCRIPT` (exit 2) — the check could not run (no transcript resolved,
  or an empty one); say so in the run index. Not `CLEAN`.

Every refusal counts, whatever issued it: a permission rule, a PreToolUse
hook, the auto-mode classifier or its being unreachable, a permission prompt
that was not approved, an interrupted call. The stderr line names the kinds
found. The scan covers the turn's transcript and those of every agent the turn
started, at any depth — through the Agent tool or a Skill that forks one.

Sending and recording it:

- `<S>` is the attempt's stem (P1): one file per attempt, so a re-run never
  overwrites an earlier verdict. The file's first line is the verdict; the
  lines after it name the description and the transcript scanned.
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

The check covers only what the transcripts hold when it runs; background work
a turn leaves running past its reply is not covered. It is evidence about
calls already made — post-hoc, never the pre-delegation permission gate, and
never a way to turn a route that gate stopped into a permitted one.

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

Under `--permission-prompts none` the P2 command `exec bash …` was refused even
with an exact allow rule (`'exec' evaluates arguments as shell code`; Claude
Code 2.1.286, 2026-10-02, one run), so such a run stops `blocked` at its first
turn (Wakes).

## Stop and restart (Execution step 4)

The moves of `SKILL.md`'s restart gate on this harness. `<scripts>` is
`<this skill's dir>/scripts`. The two gate scripts run on `node`; without it
no stop is confirmed and no judge is checked, so nothing runs again.

On `TIMEOUT`, on `STALL`, or on a reply whose last line is not a well-formed
`status:` line:

1. **Completion check.** A reply in hand whose last line is a well-formed
   `status:` line classifies the turn; the verdict was stale. Stop here.
2. **TaskStop** the turn's Agent task, right after that check — unless its
   malformed reply has arrived: that turn has ended, and there is nothing to
   stop. Keep what it reported for move 3: `stopped` (it reports the task
   stopped or killed); `completed` (nothing was running, or the reply had
   arrived); `failed` (TaskStop errored or could not be called).
3. **Observe the stop.** It blocks for at most 60 s:

   ```
   node <scripts>/stop_observe.js cc --stem <S> --desc "<the attempt's delegation description, verbatim>" --task-stop <stopped|completed|failed> --cause <deadline|ended> --deadline <seconds>
   ```

   `--cause deadline` whenever a `TIMEOUT` fired for the attempt; `ended`
   otherwise (`STALL`, or a malformed reply with no `TIMEOUT`). `--deadline`
   is the deadline this attempt's watchdog ran under: its `--deadline`, else
   its mode's `DEADLINE_*` at the top of `scripts/watchdog.sh`
   (`DEADLINE_DEFAULT` for a mode with no row; a `TIMEOUT` message also names
   it). `CONFIRMED` (exit 0) needs all of these: TaskStop reported `stopped`
   or `completed`; the transcripts of the turn and of every agent it started
   were found, and none changed during the window; none holds a call without
   a result, a call that started background work, or a call refused or
   interrupted. Anything else is `UNCONFIRMED`, no verdict word included
   (exit 64: usage, 70: internal error). It returns at once when the stop
   cannot be confirmed from the start (TaskStop not `stopped` or `completed`,
   no transcript, a call open at the stop), otherwise after the full window. A
   verdict missing from the tool result: run the same command again — the
   first verdict is recorded in `<S>-stop.json` and answers every later call.
4. **Completion check again.** A reply in hand now whose last line is a
   well-formed `status:` line is the one the turn can still be classified by;
   one that arrives later is late (**Late reply**).
5. **Denial check** for the attempt, as its own message (above). It runs after
   move 4, so it covers the reply you act on.
6. **Adopt or gate.** A reply from move 4 is adopted: classify it (Execution
   step 3) and record the verdict as superseded — unless move 5 printed
   `DENIED` on an attempt you called TaskStop for (**A stop is not a
   denial**). Otherwise the turn is `aborted`. `DENIED` → no restart, with
   the status **A stop is not a denial** gives; read it first, and that status
   holds even when the stop is also `UNCONFIRMED`. `UNCONFIRMED`,
   `NO-TRANSCRIPT` (the judge would have nothing to read), or a turn key that
   already ran again → no restart, `needs-human`. Otherwise issue the judge:

   ```
   node <scripts>/judge_gate.js issue --stem <S> --deliv <the attempt's deliverable path, or ->
   ```

   It prints one line, `<prompt> (judge, model=ultra, tools=Read,Grep,Write,
   record=<record>, timeout=<seconds>, description=<d>)`; anything else (a
   refusal, exit 1; exit 64 or 70) is `needs-human`. The timeout counts from
   `issue`, so delegate the judge in the very next message: a
   `general-purpose` Agent call in the background, with the model the `ultra`
   policy class resolves to under the delegation contract's source priority
   (the profile row is the default), the printed description
   verbatim, and only this message: `Read <prompt> and execute its
   instructions. Use only these tools: Read, Grep, Write. Reply to me with the
   single line it asks for.` In the same message start its watchdog:

   ```
   exec bash <this skill's dir>/scripts/watchdog.sh --deliv <record> --desc "<the printed description>" --deadline <the printed timeout>
   ```

   On its reply, stop that watchdog and check the reply:

   ```
   node <scripts>/judge_gate.js check --stem <S> --reply "<the judge's reply, verbatim>"
   ```

   `ALLOW <seconds> <record>` (exit 0) → move 7. `REFUSED` (exit 1), no
   verdict word, or the judge's watchdog firing first (TaskStop the judge) →
   `needs-human`. Record one line in the run index prose: the judge's
   description, the check's output line, the record path.
7. **Re-run** on `ALLOW`, keyed as below.

**A stop is not a denial.** A TaskStop that cuts a running call can leave
that call recorded as a refused one, and `deny_scan.sh` counts it. Observed
once (2026-10-02, Claude Code 2.1.284): a turn stopped at its `TIMEOUT`
scanned `DENIED 1`, kind `user-rejected`, its one entry stamped the second of
the stop (attributed by that time alone; the transcript body was not read).
The check cannot tell such a cut from a permission refusal, so on an attempt
you called TaskStop for, whatever it reported, `DENIED` cannot be read as a
permission denial: adopt no reply over it, do not restart, and stop with
`needs-human`, not `blocked`, recording the verdict line, the kinds its
stderr names, and that the attempt was stopped. With no TaskStop called (a
malformed reply ended the turn, or TaskStop could not be called), `DENIED`
reads as on any turn: `blocked`.

### Late reply

There is no grace window past the deadline (ruled 2026-10-02): a reply counts
only if it is in hand at move 1 or move 4. A reply that arrives after move 4
never classifies the turn; record it as late. After a `CONFIRMED` stop it also
shows that the stop was not real: record that, TaskStop whatever of the turn
still runs (its judge, its re-run), and stop with `needs-human`.

### Re-run keying

- The same assembled prompt, except its deliverable line, which names the
  attempt's own path (Execution step 4); its `attempt` record's `file` and its
  watchdog's `--deliv` carry the same path. The earlier attempt's files are
  left as they are.
- A **distinct description** (e.g. suffix `(re-run)`). The dead attempt's
  record is still recent; a reused key latches the new watchdog onto a
  transcript that stopped growing → immediate false `STALL`.
- Its watchdog adds `--deadline <the seconds on the ALLOW line>`. A re-run
  after a launch rejection has no ALLOW line and keeps its mode's deadline.

## Out-of-scope note

The delegation-plan validator `scripts/delegation_plan.js`
(`delegation_plan_test.sh`) is the one further helper the skill carries; it
checks the v2 ledger, a planned call and the audit trail on data only. The
rest of this note covers the P2/denial/restart helpers, each with its test:
`scripts/watchdog.sh` (`watchdog_test.sh`), `scripts/deny_scan.sh`
(`deny_scan_test.sh`), and the
restart gate's `scripts/stop_observe.js` (`stop_observe_test.sh`) and
`scripts/judge_gate.js` (`judge_gate_test.sh`), which load `gate_util.js`,
`proc_identity.js` and `cc_transcripts.js`. They are scripts because the
wall-clock bound, the denial evidence, the stop evidence and the judge's
checks must behave identically every turn; prompt assembly and the Step 0
gate stay in-prompt. The first two need bash, the gate's two need `node`.

## Residual risks specific to this harness

- The watchdog proves a turn exceeded a budget, never that it was wrong;
  thresholds are calibrated to over-wait.
- A watchdog not stopped within the stall threshold of the child completing
  emits a stale `STALL`; the stale-verdict rule discards it — noise, not a
  defect.
- `STALL` cannot tell a hang from thinking or a long tool call. The threshold
  sits above the longest expected tool call, so a real hang is detected late
  rather than never.
- A `CONFIRMED` stop speaks only for the transcripts of the turn and of the
  agents it started: processes its calls left running, detached or not, and
  anything done outside files are not observed (the stop record names them).
  How soon Claude Code stops writing a transcript after TaskStop is
  unmeasured.
- A stop that cuts a running call, or a turn that started background work,
  stays `UNCONFIRMED`, so expect a deadline stop here to end at `needs-human`
  more often than in a re-run (the one real stopped turn checked, 2026-10-02,
  came out `UNCONFIRMED`).
