# mode-orchestrator — non-runtime docs

Non-runtime documentation for the `mode-orchestrator` skill. The LLM-facing
execution spec is `SKILL.md` under `skills/mode-orchestrator/` (not here).

| Doc | Audience | Purpose |
|---|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | End users | How to use the skill: flags, modes, effort/model selection, explicit workflow-step binding, canonical index JSONL, failure loop, decision loop (`needs-decision` and `--decider`), turn watchdog and its thresholds, workflow specs, artifacts. |
| [USER_GUIDE_ja.md](USER_GUIDE_ja.md) | End users (JA) | Japanese version of the user guide. |
| [WORKFLOW_SPEC_AUTHORING.md](WORKFLOW_SPEC_AUTHORING.md) | Spec authors | How to author a `workflows/<name>.md` spec: required sections, optional turn-facing policy, explicit workflow-step binding, effort precedence, and adding a task type. |
| [AUTHORING_CONTRACT.md](AUTHORING_CONTRACT.md) | Skill maintainers | Dual-harness (Claude Code / Pi) integrity obligations, execution-profile and external adaptive-audit gates, relationship to xml-wf (including the Pi CLI launch contract and the restart gate the two skills align), `modes/` propagation pointer, and how to actually pin the skill version when measuring a prompt change on Pi. |

Runtime files (read during execution) live inside the skill dir:
`SKILL.md`, `modes/`, `workflows/` (the bundled `dev` and `research` specs),
`references/` (harness-specific delegation and time-bound mechanics — `SKILL.md`
Step -1 resolves Claude Code vs. Pi and reads `harness-cc.md` or
`harness-pi.md` accordingly), `scripts/watchdog.sh` (Claude Code only —
started per turn to bound it in wall-clock time; Pi uses the `bash` tool's
native `timeout` instead, see `references/harness-pi.md`), and
`scripts/deny_scan.sh` (Claude Code only — run after each turn's status line,
and again inside the restart gate for an aborted attempt, to detect permission
denials the turn did not report; Pi has no permission layer, so no counterpart
exists), and the restart gate's `scripts/stop_observe.js` and
`scripts/judge_gate.js` (run on `node` on both harnesses, with the helpers
`gate_util.js`, `proc_identity.js` and `cc_transcripts.js` they load).

`scripts/watchdog_test.sh`, `scripts/deny_scan_test.sh`,
`scripts/stop_observe_test.sh`, `scripts/judge_gate_test.sh`,
`scripts/pi_reply_test.sh`, and `scripts/delegation_plan_test.sh` ship
alongside those scripts but are not
runtime. Run them with `bash scripts/<name>_test.sh` after touching the script
or profile they cover. The profile gate validates execution-profile **shape**,
not content — bound models are freely editable; the watchdog, denial,
restart-gate and delegation-plan suites build their own fake session logs, ledgers and records, so they need
no `claude` CLI and no prior run. Adaptive routing on the v1 path additionally requires the external evidence checker's
registered `--self-test` (it does not apply to v2 runs); see `AUTHORING_CONTRACT.md`.

Run `bash skills/mode-orchestrator/scripts/pi_launch_test.sh` from the repository
root after changing Pi's documented launch or preflight. It executes the marked
launch with a local CLI fixture and the real extractor, checks prompt-file
refusal/fidelity, and on Windows exercises the real PID recorder and observer.
It does not call a model. Run the test knowledge gate first.
