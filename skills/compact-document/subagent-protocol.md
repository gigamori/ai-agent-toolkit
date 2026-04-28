# SubAgent Delegation Protocol

## File Structure

```
compact-document/
├── SKILL.md
├── subagent-protocol.md
├── chunk-brief/
│   └── prompt.md
├── render/
│   └── prompt.md
└── references/
    ├── shared-contract.md
    └── mode-definitions.md
```

## Prompt Template Structure

| Order | Section | Required | Content |
|-------|---------|----------|---------|
| 1 | `<role>` | Yes | SubAgent role and scope |
| 2 | `<rules>` | Yes | Quality criteria, prohibitions, output constraints |
| 3 | `<context>` | Yes | Context Handoff read instructions |
| 4 | `<task>` | Yes | Execution procedure + task_description |
| 5 | `<constraints>` | Yes | Meta-constraints |

## Runtime Variables

| Variable | Resolution Timing |
|----------|------------------|
| `{context_handoff_path}` | After Discovery completes |
| `{task_description}` | At SubAgent launch |
| `{output_file_path}` | At SubAgent launch |

Resolution steps:
1. Read prompt.md for the target task type
2. Resolve runtime variables
3. Pass the resolved template to the Task tool's prompt parameter

## Workflow

```
Discovery (main thread)
├── Receive source, inspect lightweight signals
├── Propose and confirm mode
├── Determine compaction axes
├── Decide chunking, split chunks
└── Create Context Handoff

Execution (per routing decision)
├── [no chunking] Delegate directly to render SubAgent
└── [chunking]
    ├── chunk-brief SubAgent x N (parallel, max 4)
    ├── Merge chunk briefs (main thread)
    └── Delegate to render SubAgent

Integration (main thread)
└── Present final output to user
```

## Routing

| Criterion | single (render only) | multi (chunk-brief + render) |
|-----------|---------------------|------------------------------|
| chunking | off | on |
| chunk count | 0 | 2+ |

## SubAgent Launch Spec

- Task tool, subagent_type="general-purpose"
- No model override (inherits parent model)
- chunk-brief: independent tasks, run in parallel (max 4)
- render: single invocation after all chunk processing completes
