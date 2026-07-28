# mode-orchestrator — non-runtime docs

Non-runtime documentation for the `mode-orchestrator` skill. The LLM-facing
execution spec is `SKILL.md` under `skills/mode-orchestrator/` (not here).

| Doc | Audience | Purpose |
|---|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | End users | How to use the skill: flags, modes, model override, failure loop, turn watchdog and its thresholds, workflow specs, artifacts. |
| [USER_GUIDE_ja.md](USER_GUIDE_ja.md) | End users (JA) | Japanese version of the user guide. |
| [WORKFLOW_SPEC_AUTHORING.md](WORKFLOW_SPEC_AUTHORING.md) | Spec authors | How to author a `workflows/<name>.md` spec: required sections, weak coupling, model precedence, adding a task type. |

Runtime files (read during execution) live inside the skill dir:
`SKILL.md`, `modes/`, `workflows/` (e.g. the bundled `dev` spec), and
`scripts/watchdog.sh` (started per turn to bound it in wall-clock time).

`scripts/watchdog_test.sh` ships alongside the watchdog but is not runtime: run
it with `bash scripts/watchdog_test.sh` after touching the watchdog or its
thresholds. It builds its own fake session logs, so it needs no `claude` CLI and
no prior run.
