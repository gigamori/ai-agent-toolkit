# SubAgent Delegation Protocol

## File Structure

```
{skill-name}/
├── SKILL.md
├── {task-type-a}/
│   └── prompt.md             # prompt template for task type a
├── {task-type-b}/
│   └── prompt.md             # prompt template for task type b
├── references/               # runtime-read docs (read or sliced/inlined during execution)
│   └── mode-definitions.md
└── only_for_human/           # authoring-only docs (not read at runtime)
    └── shared-contract.md
```

Files under `only_for_human/` are authoring-only references (e.g. a shared contract inlined into the prompt templates at authoring time) and are NOT read at runtime; `references/` holds docs that ARE read at runtime.

## Prompt Template Structure

| Order | Section | Required | Content |
|-------|---------|----------|---------|
| 1 | `<role>` | Yes | SubAgent role and scope |
| 2 | `<rules>` | Yes | Quality criteria, prohibitions, output constraints |
| 3 | `<context>` | Yes | Context Handoff read instructions |
| 4 | `<task>` | Yes | Execution procedure + task_description |
| 5 | `<constraints>` | Yes | Meta-constraints |

This order is deliberate, not just conventional: `role`/`rules` establish
criteria and boundaries *before* the model commits to executing `task`. See
`instruction-writing-tips.md` § Ordering for why front-loading criteria this
way improves reliability.

## Runtime Variables

| Variable | Resolution Timing |
|----------|------------------|
| `{context_handoff_path}` | After Discovery completes |
| `{task_description}` | At SubAgent launch |
| `{output_file_path}` | At SubAgent launch |

Resolution steps:
1. Read prompt.md for the target task type
2. Resolve runtime variables
3. Pass the resolved template as the prompt of the subagent delegation tool

## Workflow

```
Discovery (main thread)
├── Confirm requirements and inputs with the user
├── Decompose the work into subtasks per task type
└── Create Context Handoff

Execution (per routing decision)
├── [single subtask] Delegate directly to one SubAgent
└── [multiple subtasks]
    ├── Independent SubAgents x N (parallel, max 4)
    └── Merge intermediate results (main thread or Integration SubAgent)

Integration (main thread)
└── Present final output to user
```

## Routing

| Criterion | single | multi |
|-----------|--------|-------|
| independent subtasks | 1 | 2+ |
| merge/integration step | not needed | after all subtasks complete |

## SubAgent Launch Spec

- Use the subagent delegation tool (e.g. Task/Agent tool), general-purpose subagent type
- No model override (inherits parent model)
- Independent subtasks: run in parallel (max 4)
- Integration/final task: single invocation after all subtasks complete
