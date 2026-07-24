# mode-orchestrator — non-runtime docs

Non-runtime documentation for the `mode-orchestrator` skill. The LLM-facing
execution spec is `SKILL.md` under `skills/mode-orchestrator/` (not here).

| Doc | Audience | Purpose |
|---|---|---|
| [USER_GUIDE.md](USER_GUIDE.md) | End users | How to use the skill: flags, modes, model override, failure loop, workflow specs, artifacts. |
| [USER_GUIDE_ja.md](USER_GUIDE_ja.md) | End users (JA) | Japanese version of the user guide. |
| [WORKFLOW_SPEC_AUTHORING.md](WORKFLOW_SPEC_AUTHORING.md) | Spec authors | How to author a `workflows/<name>.md` spec: required sections, weak coupling, model precedence, adding a task type. |

Runtime files (read during execution) live inside the skill dir:
`SKILL.md`, `modes/`, and `workflows/` (e.g. the bundled `dev` spec).
