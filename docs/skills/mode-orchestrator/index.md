# mode-orchestrator — non-runtime docs

Non-runtime documentation for the `mode-orchestrator` skill. The LLM-facing
execution spec is `SKILL.md` under `skills/mode-orchestrator/` (not here).

| Doc | Audience | Purpose |
|---|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | End users | How to use the skill: flags, modes, model override, failure loop, decision loop (`needs-decision` and `--decider`), turn watchdog and its thresholds, workflow specs, artifacts. |
| [USER_GUIDE_ja.md](USER_GUIDE_ja.md) | End users (JA) | Japanese version of the user guide. |
| [WORKFLOW_SPEC_AUTHORING.md](WORKFLOW_SPEC_AUTHORING.md) | Spec authors | How to author a `workflows/<name>.md` spec: required sections, weak coupling, model precedence, adding a task type. |
| [AUTHORING_CONTRACT.md](AUTHORING_CONTRACT.md) | Skill maintainers | Dual-harness (Claude Code / Pi) integrity obligations, relationship to xml-wf, `modes/` propagation pointer, and how to actually pin the skill version when measuring a prompt change on Pi. |

Runtime files (read during execution) live inside the skill dir:
`SKILL.md`, `modes/`, `workflows/` (e.g. the bundled `dev` spec),
`references/` (harness-specific delegation and time-bound mechanics — `SKILL.md`
Step -1 resolves Claude Code vs. Pi and reads `harness-cc.md` or
`harness-pi.md` accordingly), `scripts/watchdog.sh` (Claude Code only —
started per turn to bound it in wall-clock time; Pi uses the `bash` tool's
native `timeout` instead, see `references/harness-pi.md`), and
`scripts/deny_scan.sh` (Claude Code only — run once after each turn's status
line to detect permission denials the turn did not report; Pi has no
permission layer, so no counterpart exists).

`scripts/watchdog_test.sh` and `scripts/deny_scan_test.sh` ship alongside
those scripts but are not runtime: run them with `bash scripts/<name>_test.sh`
after touching the script they cover. Both build their own fake session logs,
so they need no `claude` CLI and no prior run.
