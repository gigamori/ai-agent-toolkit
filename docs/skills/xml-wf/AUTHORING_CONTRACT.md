# Maintenance contract — xml-wf

> Positioning: this is **maintenance context, not execution context** — not needed to *run* xml-wf, needed to *maintain* it. `SKILL.md` and `references/spec.md` are behaviorally accurate for execution; this file records the obligations an editor of the skill must satisfy that those files do not (and should not) state.

## Multi-facet integrity

xml-wf executes steps through three interchangeable facets, selected by mode/backend: **Run (batch)** (`--backend cc`, `references/run-cc.md`), **Run (batch, Pi)** (`--backend pi`, `references/run-pi.md`, driven by `scripts/wfrun/pi_cli.py`), and **LLM orchestration** (`--run-llm`, `references/run-llm.md`: layer B only, with every step running through the orchestrating agent's own delegation facility on any harness, including Pi with no subagent tool, where it is a `subagent` tool the host publishes if that qualifies and otherwise a `pi -p` process). `wfrun dispatch`/`wait` (layer A) remain in `scripts/wfrun/__main__.py` but are not part of this mode.

The `pi` backend is not a thin mirror of `cc`: it refuses some attributes outright (`schema=`, for one, because it cannot enforce structured output) and rewrites others. `references/run-pi.md` enumerates every such divergence and is the source of truth for all of them — read it there, and when a divergence changes, change it there.

Because Pi loads this skill via a symlink into `~/.pi/agent/skills/` (not a copy), a change reaches Pi automatically — there is no propagation step to remember. What is NOT automatic is correctness on the Pi side:

- Before landing a change to `scripts/wfrun/executor.py` (or anything the control-flow spec governs — error handling, retry, `<replan>`, variable resolution), judge explicitly whether `scripts/wfrun/pi_cli.py` and `references/run-pi.md` still hold, and update both if the pi backend's behavior or its documented rewrites now differ.
- Run the full test suite from the skill's `scripts/` directory (`uv run python -m unittest discover -s tests`) after touching `executor.py`, `pi_cli.py`, or anything under `scripts/wfrun/modes/` — the suite includes `test_pi_cli.py`, `test_run_backend.py`, and `test_run_inherit_model.py`, which are the only mechanical check that the pi facet did not silently regress.
- A change scoped to one facet only (e.g. a `run-llm` layer-B delivery fix) needs no edit to the others — but state that scoping decision, don't leave it implicit.
- After editing `scripts/wfrun/modes/*.md` or the prompt assembly, sample the prompt layer (`uv run python evals/prompt_smoke.py`, also from `scripts/`) and compare compliance rates before/after — that layer is probabilistic and outside the unit tests.
- Injected guardrail text lives in `scripts/wfrun/guardrails.py` and nowhere else, including the conditionally injected `VALUE_LINE_RULE` and the `VALUE_LINE_PREFIX` / `VALUE_LINE_PLACEHOLDER` tokens its extractor compares against: `stepio` holds the *condition* and the parsing, never a string literal of the prompt. This is what keeps the A/B control cheap — a **wording** change is measured by restoring `guardrails.py` alone in a worktree, and only a change to the injection **condition** needs `stepio.py` in the before-condition too. Wording that migrates into `stepio.py` silently escapes that control.

## Refused-call detection

A step that had a tool call refused never counts as a success. That holds whatever refused the call: a permission rule, a PreToolUse hook, an approval that print mode cannot give, or the auto-mode classifier. It also holds wherever the call happened: in the step itself or in any agent it started. Each facet learns of it differently, and an editor must keep all of them true:

- **Run (batch) on cc, and layer A's `dispatch`/`wait` (not part of run-llm)**
  - `claude_cli.classify_result` reads `permission_denials` from every `type:"result"` event of the child's `--output-format stream-json` output, and takes their union.
  - The union is load-bearing. A refusal inside a background agent is listed only on the result of the turn that started it, never on the last one, and `--output-format json` printed only the last.
  - A result that would pass while an event lacks the field is `unchecked`, never retried or debugged. The field was on every result measured, so its absence means the CLI changed.
  - Layer A's `dispatch` refuses to re-dispatch a cycle whose last attempt was `denied`.
- **run-llm layer B on Claude Code**
  - There is no result JSON. `record`, and after an abort `wfrun stop`, `wfrun judge` and `prompt --redispatch`, read the step subagent's own transcript through `wfrun/ccdeny.py`.
  - It finds the subagent by the per-attempt Agent description `wfrun prompt` issues, looking only among the calling session's agents.
  - It adds every agent that subagent started, at any depth, and counts lines carrying a top-level `toolDenialKind`.
  - A transcript that cannot be resolved is reported as NO-TRANSCRIPT, and the step keeps its status. It is never read as clean, and it stops a restart: `wfrun judge` and `prompt --redispatch` refuse an attempt whose transcript cannot be found.
  - A denied step is not re-run: `prompt` refuses it in code (`_admit_step_attempt`, through `_admission_refusal`), and `run-llm.md` says so as well. After a recorded stop, a refusal found in the transcript is not read as a denial: `record` records the attempt `aborted` and prints the check's verdict on a `deny-check:` line, and `wfrun judge` and `prompt --redispatch` refuse to restart an attempt whose transcript holds a refused call.
- **Pi (run-pi, layer B on Pi):** there is no check. pi has no permission layer. An extension could block a tool call, but the block surfaces as an ordinary tool error with no marker to key on, and none is installed by default (`run-pi.md`).

**Every refusal kind counts the same.** `denied` is one class, and this skill does not split "refused" from "did not run". That agrees with mode-orchestrator's ruling of 2026-09-25, so there is no divergence to record. One case is read differently in layer B: a call that a stop cut (the orchestrator's TaskStop) can stay recorded as refused, and once the stop is recorded, `record` records the attempt `aborted`, not `denied`, with the check's verdict on a `deny-check:` line — a stop is not a denial, the rule mode-orchestrator's `harness-cc.md` states under the same name. `evals/deny_canary.py` does not reproduce such a call.

**Two signals, on purpose:**
- The batch facets key on `permission_denials`. It is the only refusal signal `claude -p` output carries: `toolDenialKind` exists only in transcripts, and `--no-session-persistence` writes none.
- Layer B keys on `toolDenialKind`.

Both go inert silently if Claude Code changes its output, and the unit tests cannot notice, because their fixtures are fixed. **After every Claude Code upgrade**, run `uv run python evals/deny_canary.py` from `scripts/`. It calls the real CLI and costs money. It reproduces a direct refusal, a hook refusal, a refusal inside an agent, the transcript field layer B reads, and a clean control, and it fails if any of them no longer classifies as measured. The measured facts behind these rules live in [`docs/dev/test-constraints.md`](../../dev/test-constraints.md).

**Relationship to mode-orchestrator:** the idea came from its `deny_scan.sh` (sibling handoff of 2026-09-25). The implementations are independent, and no propagation obligation follows in either direction.

## Run-dir file invariant

**I1** (2026-09-29 user ruling, `xml-wf-kill-safe-cleanup.md` §1/§2): wfrun
never creates a file outside the run dir to call a CLI. Every file a runner
hands to `claude`/`pi` — a system prompt, a debug or adjudicator role — is a
run artifact: written inside `runs/<name>_<ts>/` under a deterministic name
(`steps/<id>_<attempt n>/`, `steps/<id>_cNN_*` — see `__main__._a_layer_paths`
— or `decisions/`), never cleaned up, and therefore never leaked when the
process is killed rather than allowed to run its `finally` block. Same
principle as mode-orchestrator's `watchdog.sh` fix
(`watchdog-taskstop-leak.md`).

This replaced the original design (`system_prompt` text written to
`tempfile.mkstemp()` in the OS temp dir, deleted in a `finally: os.remove()`):
a hard kill — win32 TaskStop, `taskkill /F`, a crash — skips `finally`, so the
temp file survived the process (`xml-wf-killable-tempfile-audit.md`, defects
1–2).

An editor adding a new CLI invocation (a new runner call site, or a new
diagnose/adjudicate-style helper that assembles its own role/system text)
must:

- Never write the file the CLI reads to a system temp directory
  (`tempfile.mkstemp`/`NamedTemporaryFile`, `%TEMP%`, `/tmp`) or delete it in
  a `finally`/`atexit` block — that pattern is exactly what I1 forbids,
  because a hard kill skips it.
- Route the payload through `system_prompt_file=` (optional keyword on
  `claude_cli.run_claude`/`pi_cli.run_pi`, falls back to inline when omitted)
  or `role_file=` (keyword-only, **no default**, on `adp.diagnose` and
  `adjudicate.adjudicate`/`adjudicate_text`/`adjudicate_pi` — the missing
  default is deliberate: a call site that forgets to pass it fails with
  `TypeError` instead of silently reverting to inline) to a deterministic path
  inside the run dir. A caller that goes through `run_claude`/`run_pi` gets
  absolute-path resolution and the shim-hostile-path check below for free —
  they live inside `_launch`/`run_pi` themselves, not at the call site.
- A new launcher written *alongside* `_launch`/`run_pi` (rather than calling
  through them) has to reimplement both of the following itself: resolve the
  file path to an absolute one (`Path(...).resolve()`) before handing it to
  the CLI, since the child runs with `cwd=base_dir`, which a relative run-dir
  path would not survive; and, when the call goes through `via_shim`, apply
  `_has_hostile_metachars` to the resolved path string itself (not only the
  file's content) — a run-dir path can contain shim-hostile characters (`%`,
  `&`) that a `%TEMP%`-only path never could.

## Parent-death binding

**I2** (`xml-wf-kill-safe-cleanup.md` §1/§3.4): however the wfrun process that
launched an LLM CLI ends — `taskkill /F` on its PID, a crash, any hard kill —
that CLI must not keep working. An unbound child does keep working: measured
2026-09-29 on win32 (claude v2.1.270), a `claude -p` whose parent was killed
finished its in-flight tool call, then ran the next one, and a resumed or
re-dispatched step would have run alongside it. The OS mechanics live in
`scripts/wfrun/procbind.py`, whose only entry point is `popen_bound`:

- Windows: a kill-on-close Job Object. The CLI and everything it starts end
  with the parent. The binding is released once `wait()` has seen the CLI
  exit, so a normal exit behaves as before.
- Linux: `PR_SET_PDEATHSIG`. Binds the CLI itself, not its descendants. Not
  yet run on a Linux host: its test is skipped everywhere else.
- macOS and every other POSIX: unbound. Accepted residual (2026-09-29 user
  ruling).

Bound today: `claude_cli._run_bound` (every `run_claude` call without
`kill_tree`: steps, replans, `ask_llm`, `adp.diagnose`, `adjudicate`) and
`claude_cli._run_with_tree_kill` (the A-layer wrapper and every `run_pi`
call).
Still unbound, outside I2's scope when it was introduced:
`pi_cli.ask_llm_pi`, `pi_cli.list_available_models`, the capability probe in
`claude_cli._supports_system_prompt_file`, and `evals/deny_canary.py`.

An editor adding an LLM CLI launch goes through `run_claude`/`run_pi`, or
calls `procbind.popen_bound` directly. A plain `subprocess.run`/`Popen` there
reopens I2.

## Layer A liveness and admission

**I3** (2026-09-29): `wfrun dispatch` and `wfrun wait` decide whether a step's
latest attempt is running, overdue, gone or done from evidence, and both must
reach the same verdict for the same attempt, which is why both go through
`_attempt_state`. The machinery is `cmd_dispatch`, `cmd_wait`,
`cmd_run_wrapper` and `_attempt_state` in `scripts/wfrun/__main__.py`, over the
lock driver `scripts/wfrun/runlock.py`. It is layer A only: run-cc, run-pi and
layer B (`prompt`/`record`/`poll`) neither share it nor change with it; run-cc
and run-pi use the same lock driver, but only for run-dir exclusivity (I4). An
editor of any of it keeps four things true:

- **Admission before launch.** `cmd_run_wrapper` takes the step's run lock and
  writes the marker carrying its own token and pid before it calls `_launch`.
  On success its last write is `exit.json`, and the run lock is released only
  after it. On an exception the lock is released anyway, in a `finally`, and
  that is how an observer comes to see FREE and settle the attempt as gone. A
  wrapper that cannot show it is the current attempt (for example the handle's
  token is not its own, its seq is already in the ledger, or the run lock is
  held elsewhere) exits without writing anything or launching. The one
  exception is a filesystem where the lock call itself is unsupported: every
  participant falls open there, and liveness reverts to the clock.
- **`_attempt_state` reads in a fixed order:** `exit.json`, then the marker,
  then the run-lock probe, and the ledger only once the probe is not HELD.
  Reading the marker after the probe lets `dispatch` launch a second wrapper
  beside one that is being admitted. Reading the ledger while the lock is held
  can catch its replace mid-write on Windows and read a real attempt as absent.
- **A PID is never evidence.** It can be reused, and behind a uv venv launcher
  `Popen.pid` is not the wrapper's
  ([`docs/dev/test-constraints.md`](../../dev/test-constraints.md)). The run
  lock and the marker's token decide, then the ledger and `exit.json`, and only
  last the clock. This is layer A's rule. Layer B's Pi CLI stop (`wfrun pids`,
  `wfrun stop --facet pi-cli`) does record PIDs, but as (PID, image, creation
  time) identities (`procid.py`), and it counts any doubt as still running.
- **Locks are taken state first, run second, never the reverse.** The wrapper
  retries for the run lock, within a bound, while holding the state lock, and
  nothing takes the state lock while holding the run lock. Observers only probe
  the run lock, without blocking.

## Run-dir exclusivity (run-cc and run-pi)

**I4** (2026-09-30): one live `wfrun run`/`resume` process per run dir.
`cmd_run` and `cmd_resume` in `scripts/wfrun/__main__.py` take
`<run-dir>/run.lock` through `_lock_run_dir` (the `runlock` driver of I3) and
run their bodies inside a `finally` that releases it. Both facets share this
code, so an editor keeps these true for cc and pi alike:

- **Lock before the first write, release after the last.** `run` locks right
  after creating the run dir and before copying `workflow.xml`; `resume` locks
  before it reads the events or appends an `--answer`. The release sits in the
  `finally`, after the report. A write moved outside that span reopens the
  double execution: a second `resume` replays up to the step the first process
  is still running and runs that step again.
- **A contended lock refuses with exit 2 before anything is written or
  launched.** Exit 2 means "rejected before anything ran; the run dir is as it
  was", shared with an unusable `--answer`. It must never become 1, which
  run-cc.md answers with `resume`.
- **Unsupported locking falls open with a `note:`, any other lock error with a
  `warning:`** — the same policy as layer A's `dispatch`.
- **The lock only proves that the wfrun process is alive.** Known gaps,
  accepted for now:
  - When that process is killed, children I2 does not bind (macOS, a CLI's own
    descendants on Linux, the unbound calls listed under I2) can keep running
    while the lock reads FREE, and a new `resume` then runs beside them.
  - `_run_with_tree_kill` (every `run_pi` call) does not kill its child on an
    interrupt, so after a Ctrl+C on macOS the pi child outlives the released
    lock. The fix belongs to I2.
  - A second Ctrl+C during a `<parallel>` pool shutdown can reach the
    `finally` while worker threads of the same process still write events
    (inferred from source, not reproduced).
  - Windows frees a dead holder's lock only after a few milliseconds
    ([`docs/dev/test-constraints.md`](../../dev/test-constraints.md)), so a
    `resume` started immediately after a kill can still be refused with exit
    2; retrying is correct.
  - Layer A's `dispatch`/`wait` and layer B's `prompt`/`record`/`poll` do not
    take this lock, so a run-llm step and a batch run of the same run dir are
    not kept apart.

## Test knowledge gate

The test files under `scripts/tests/` carry no comments and no docstrings — they are read by
agents that hold only this checkout, and prose no run ever prints reads to them as fact.
The two rules, the directive allowlist, the running order and the migration record live in
[`docs/dev/test-gate.md`](../../dev/test-gate.md); do not restate them here.

What an editor of this skill has to do:

- Run `uv run --no-project python tests/lint_test_knowledge.py` from the checkout root
  before the suites. Exit 2 means it scanned nothing, which is a coverage failure and not a
  clean tree.
- A fact about the world outside this repository — a CLI's observed behaviour, a platform
  encoding default, a measured model rate — goes to
  [`docs/dev/test-constraints.md`](../../dev/test-constraints.md) with the date it was
  observed, never into the test file. That document is its only home.
- The intent behind a counter-intuitive assertion goes into the assertion message, where a
  failure prints it. If it needs more than one line, the test is too complex to operate.
- `scripts/evals/` is deliberately outside the gate: those harnesses spend money and the
  ordinary change loop does not run them. Fold one into that loop and it is in scope the
  same day.

## Relationship to mode-orchestrator

xml-wf and `skills/mode-orchestrator/` are separate products that independently reimplement the same idea — running an isolated, mode-tagged turn per step — over different substrates (xml-wf: `wfrun`'s deterministic Python control flow over `<step>` elements; mode-orchestrator: one subagent call per todolist step). Neither is canonical for the other; there is no shared file, and — with the exceptions below — no propagation obligation between them.

When a change here alters step-execution semantics in a way that looks generally useful (e.g. how a stale completion signal is discarded, how the harness/model resolution works, retry/escalation policy), consider whether `skills/mode-orchestrator/` would benefit from the same idea and note it for its maintainer — a suggestion to evaluate, not an obligation to port.

## Pi CLI launch contract (shared with mode-orchestrator)

A further exception to "no shared file": the launch contract of a Pi CLI delegation. This skill's `references/run-llm.md` ("Delegating on Pi") and mode-orchestrator's `references/harness-pi.md` (P1) carry the same block between `<!-- pi-cli-launch-contract:begin -->` and `<!-- pi-cli-launch-contract:end -->`, and each marks its own launch command (the fenced block after `<!-- pi-cli-launch-line -->`). Two copies of one text drift unless someone checks.

- **Propagation, both directions.** An edit to the marked block here is made to the other copy in the same pass, and the other way round. Options outside the block stay each skill's own (here `--mode text`, `--tools`, `wfrun pids`; there `MODE_ORCH_DEPTH`, `--mode json`, the raw capture and `pi_reply.js`) and do not propagate.
- **Run obligation.** After touching the launch text of either document, run `uv run --no-project python tests/lint_launch_contract.py` and then the same command with `--self-test`, from the checkout root. It fails when the two blocks differ, or when either skill's launch command lacks a common element (entry through node, `-p`, closed stdin, `--no-skills`, `--session-dir`, `MSYS_NO_PATHCONV=1`, the message through `"$PROMPT"`, the recorded PIDs) or has the elements out of order. It does not see a launch command outside the marked blocks, and it does not run the CLI. Exit 2 means it scanned nothing.
- **The restart gate is aligned, not shared.** `wfrun stop`, `wfrun pids` and `wfrun judge` (`scripts/wfrun/llmgate.py`, `procid.py`, `ccdeny.py`, and `JUDGE_PROMPT` in `guardrails.py`) and mode-orchestrator's `scripts/stop_observe.js` and `scripts/judge_gate.js` implement one contract twice, on purpose: the same judge prompt body, record fields and reply grammar, the same `ultra` model class, `Read, Grep, Write` tools, 900-second deadline, one judge per attempt, every judge failure a refusal, and a 60-second window for observing a stop. The code, the tests and the files each one writes are independent, and they differ where the contract says nothing:
  - the stop's cause: here the clock (`requested_at >= dispatched_at + timeout`), there the caller's `--cause`;
  - a Claude Code observation: here it runs to the end of the window unless the result completes, there it returns at once when the stop cannot be confirmed any more; a Pi CLI observation there can also run up to 20 seconds past the window, because a process probe begun before the window ends is not cut short;
  - the verdict words: `done` / `confirmed` / `unconfirmed` (exit 0 / 12 / 13) here, `CONFIRMED` / `UNCONFIRMED` (exit 0 / 1) there;
  - the judge's end marker: `[[WFRUN-END step=<id> attempt=cNNaNN-judge-<token>]]` here, `[[MO-JUDGE-END <token>]]` there;
  - the evidence the judge may cite: here the attempt's prompt, result, stop record and transcripts, there its deliverable (if any), stop record and transcripts;
  - where the check of the judge's reply sits: inside `prompt --redispatch` here, so no restart skips it; the separate command `judge_gate.js check` there;
  - how a second restart is refused: `_admission_refusal` here, and there `judge_gate.js issue` refuses a stem that does not end in `-1`.

  Nothing detects drift between the two prompt texts or grammars: whoever changes one reads the other, and changes both or records why not.

## Decision contract (aligned with mode-orchestrator)

The demand-driven decision channel (`DECISION:` requests, `references/spec.md` "Decision requests") shares a **deliberately aligned user-facing contract** with mode-orchestrator's decision loop: the decider vocabulary (`human`/`llm`, default `human`), the cap semantics (llm rulings only — human answers never consume it), and the escalation grounds (irreversible / outward-facing / goal-changing → human). The alignment exists because users move between the two skills, and defaults that disagree tax every unattended run's post-mortem.

The implementations are independent (xml-wf enforces the contract in `wfrun` code; mode-orchestrator enforces it as prompt contract in `SKILL.md`) and some divergences are **deliberate** — e.g. xml-wf's run-llm orchestrator never reads a request body while mode-orchestrator permits a scoped read of its `## Decision request` section; xml-wf has continuation forms (a)/(b), mode-orchestrator has amend-plan; xml-wf's ledgers (`events.jsonl`, the `decisions/` verdict markers) vs mode-orchestrator's run-index records. Do not "fix" those toward each other.

**Record shape sits inside that ledger divergence — judged 2026-08-28, no action taken here.** mode-orchestrator now pins its run-index `decision` record as `key`, `parent`, `decider`, `file`, written on the `--decider=human` path only. Nothing on this side needs aligning, and the reason is structural rather than a preference: xml-wf records **every** adjudication as one verdict marker carrying a `decider` field (`stepio.py` writes `"decider": decider`; `executor.py` sets it to `DECIDER_LLM` or `DECIDER_HUMAN`), and `decision.llm_adjudications` counts the `llm` ones — the cap tally *requires* those records to exist. mode-orchestrator's cap counts inserted turns instead, so each side's tally reads its own ledger's native record and neither could adopt the other's shape without moving its cap. The consequence worth knowing before anyone "fixes" it: **there, a reader wanting every adjudication must union `decision` records with `turn` records of `kind: decision-turn` and infer the decider from that kind, because no field on a turn or attempt record carries it; here one filtered record set answers the same question.** That asymmetry is a property of the two ledgers, not a defect in either.

**Resolved 2026-08-28 — the escalate-when-unsure half of the escalation pin is now mirrored on both sides.** This side carries it in code (`guardrails.py`'s `DECIDE_ROLE`: "Escalate whenever you are uncertain"; `DECIDE_PROMPT_TEXT`: "and whenever you are unsure") and in `references/run-cc.md` and `references/run-llm.md`. mode-orchestrator's inserted-decision-turn clause names the three grounds and stops there. The three named grounds DO agree; only the when-unsure half is absent. This is pre-existing and was not caused by the record-shape change. It was closed by adding the clause to mode-orchestrator's inserted-decision-turn text, not by deleting this side's — that direction was chosen because `SKILL.md`'s own following sentence says the escalation clause is what stops an `llm` decider collapsing into deciding everything, so the missing half guarded a failure that skill already names.

**Ruled 2026-09-23 (user) — a deliberate divergence on the vocabulary and escalation pins: mode-orchestrator routes model-authored forks; this side does not.** mode-orchestrator's `--decider=human` now sends a fork whose every named source is an instruction a model wrote during the run to an inserted decision turn rather than the human, and gives that turn one extra escalation line (`needs-human` if the fork rests on an instruction a human wrote). Here `human` still presents every fork, and the adjudicator prompts (`DECIDE_ROLE`, `DECIDE_PROMPT`, `DECIDE_PROMPT_TEXT`, run-llm's delegation message) are unchanged. Not aligned because provenance on this side is decided by code: `stepio` assembles the task from the XML, and only `<replan>` continuations and a debug `fix_instruction` are model-written, so an alignment would be a `wfrun` change (a provenance field on the payload, routing in `_handle_decision` and in `record`/`wait`), not a doc edit. The vocabulary itself, the default `human`, the cap semantics and the named escalation grounds stay aligned. Do not "fix" this toward mode-orchestrator by editing docs alone.

When editing either side's decider vocabulary, defaults, cap semantics, or escalation grounds, the `generate-sibling-handoff` registry (`references/families.md`, **decision-contract** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here. Precedent: the cap-scope alignment landed as P3 (`9168a52`).

## Model-tier contract (aligned with mode-orchestrator)

`references/build.md` § Model selection declares its tier vocabulary (`basic`/`pro`/`ultra`) canonical for both this skill and mode-orchestrator, so the three tier NAMES must not drift between them — the concrete model each tier BINDS to is a deliberate exception: bindings are independent per skill (`model_map.json` here, `execution-profiles.md` there) and are expected to differ.

The implementations are independent (xml-wf enforces the vocabulary in code — `modelmap.py`'s `CANONICAL_MODELS`, `lint.py`'s `model-not-canonical`/`model-legacy-name` checks; mode-orchestrator's `execution-profiles.md` is prompt-read only, enforced by its own shape gate).

**Both sides carry `basic`/`pro`/`ultra` as of 2026-08-28.** An earlier revision of this section recorded a mid-rename period in which `execution-profiles.md` and `build.md`'s own tier-defining sentence were still pre-rename; that period ended when the deferred mode-orchestrator session landed, and those statements were corrected here in the same pass rather than left standing. The consequence that matters: **a drift check reporting that canonical and `execution-profiles.md` disagree is now a regression, not the expected state.** One deliberate non-rename remains and is not an oversight — `build.md`'s `wfrun validate` paragraph (lines 181-182, "...so `opus` reaches `opus[1m]`...") is a dated outside-world measurement and keeps the words it was measured with. See `references/families.md`'s `model-tier` family for the dated tracking.

When editing the tier vocabulary, the approved-candidates-per-layer lists, or the measured floors, the `generate-sibling-handoff` registry (`references/families.md`, **model-tier** family) owns the propagation trigger and the consumer list — consult it before landing the change, and do not duplicate its content here.

## Model choice for evals

`scripts/evals/` harnesses call a real CLI and cost money, so a `--model` default there is a maintained choice, not a placeholder. Take it from the **measurement layer** list in `references/build.md` (§ Model selection), which is canonical because `mode-orchestrator` reads the same list — do not restate the names here. When a default moves, say so at the call site: a rate measured under a different variant is not a continuation of the old figure. Precedent: `adjudicator_smoke.py` defaulted to `google/gemini-3.1-flash-lite` until 2026-08-17, and the 40/40 ruling rate `build.md` cites belongs to that variant, not to the current default.

## Mode prompt fragments

`scripts/wfrun/modes/` is an INDEPENDENT derivation of the `role-mode` plugin's canonical mode bodies (`plugins/role-mode/prompts/modes/`), not a copy: xml-wf's own 4-axis/3-axis framework headers (`Mode / Rules / Task / Role`, `[BLOCKED: rules <id>]` literal, guardrail text) exist because a `<step>` carries `<rules>` and `<task>` that canonical does not model. Only the **mode filename set** (identity — which modes exist) tracks canonical; bodies and headers are maintained here independently and must never be synced by filename match. Propagation obligations and drift detection are owned by the `generate-sibling-handoff` skill's registry (`references/families.md`, `role-mode` family) — see that registry before touching `scripts/wfrun/modes/`, and do not duplicate its content here.
