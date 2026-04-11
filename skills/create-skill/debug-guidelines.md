# Debug Session Guidelines

When a skill includes scripts or external tool execution, define how the agent should handle execution errors. The key decision is whether to debug in the main session or delegate to a SubAgent.

## Session Decision Rule

1. On first failure, attempt to fix in the main session
2. If the fix requires an iterative trial-and-error loop (multiple hypotheses, large error output, or the root cause is independent of the primary task), delegate to a `debug` task type SubAgent
3. The SubAgent isolates all intermediate noise (failed attempts, stack traces, hypothesis testing) and returns only the final result (fixed file or root cause report)

## Why This Matters

Debugging traces accumulate in the context window. Each failed attempt and error log dilutes the attention weight on the primary task state. After the debugging loop, the agent's ability to resume the main workflow degrades because attention is spread across irrelevant intermediate states.

A debug SubAgent acts as a noise barrier: the main session receives only the conclusion, preserving context quality for subsequent phases.

## Adding a Debug Task Type

If the skill uses the SubAgent Delegation Protocol, add a `debug/` task type directory alongside existing ones:

```
my-skill/
├── SKILL.md
├── subagent-protocol.md
├── table-spec/
│   └── prompt.md
├── integration/
│   └── prompt.md
└── debug/
    └── prompt.md          # Debug-specific prompt template
```

The debug `prompt.md` should follow the same section structure (`<role>`, `<rules>`, `<context>`, `<tools>`, `<task>`, `<constraints>`) as other task types. Key rules for the debug SubAgent:

- Only modify the target script/SQL; no unrelated changes
- Keep fixes minimal; no refactoring or style changes
- Always verify the fix by re-running the reproduction command before outputting
- If the fix is not possible, output root cause analysis and recommended actions instead
- Never include intermediate trial-and-error in the final output

## Immediate Termination vs Fallback

Not all errors warrant debugging. In the SKILL.md, classify errors into:

- Immediate termination (no fallback): errors caused by missing prerequisites the agent cannot fix (e.g., missing credentials, undefined environment variables, authentication failures)
- Fallback to alternative route: errors where the agent can switch approaches (e.g., unsupported tool version → manual execution)

Document both categories explicitly so the agent does not waste cycles debugging unrecoverable errors.

## Tool Naming in Prompt Templates

Use capability-based names instead of platform-specific tool names to keep prompt templates portable across agent systems:

| Capability-based name | Examples of platform-specific names |
|-----------------------|-------------------------------------|
| File reading (read-related tool) | Read, cat, file_read |
| File writing (write-related tool) | Write, file_write |
| File editing (edit-related tool) | Edit, sed, file_edit |
| OS command execution (shell/terminal-related tool) | Bash, Terminal, run_command |
| File pattern search (glob/find-related tool) | Glob, find, list_files |

The LLM maps these capability descriptions to whatever tools are available in its runtime environment.
