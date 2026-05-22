# Advanced Mode: Claude Code Extensions

Reference guide for creating skills that leverage Claude Code-specific features
beyond the [Agent Skills](https://agentskills.io/specification) open standard.

Official documentation:
- [Skills](https://code.claude.com/docs/ja/skills)
- [Sub-agents](https://code.claude.com/docs/ja/sub-agents)

## Extended Frontmatter Fields

Standard mode covers `name`, `description`, `license`, `compatibility`, `metadata`,
`allowed-tools`. In advanced mode, `name` becomes optional (defaults to directory name)
and `description` becomes recommended rather than required (falls back to first paragraph
of markdown body). Advanced mode adds:

| Field | Purpose |
|-------|---------|
| `when_to_use` | Additional trigger context appended to description |
| `argument-hint` | Autocomplete hint shown during `/` completion (e.g., `[issue-number]`) |
| `arguments` | Named positional arguments for `$name` substitution |
| `disable-model-invocation` | `true` = user-only invocation via `/name` |
| `user-invocable` | `false` = hide from `/` menu, Claude-only background knowledge |
| `context` | `fork` = run in isolated subagent context |
| `agent` | Subagent type when `context: fork` (`Explore`, `Plan`, custom name) |
| `model` | Model override (`sonnet`, `opus`, `haiku`, full model ID, `inherit`) |
| `effort` | Effort level: `low`, `medium`, `high`, `xhigh`, `max` |
| `paths` | Glob patterns limiting when skill auto-activates |
| `hooks` | Lifecycle hooks scoped to this skill's execution |
| `shell` | Shell for inline commands: `bash` (default) or `powershell` |

## String Substitutions

| Variable | Description |
|----------|-------------|
| `$ARGUMENTS` | All arguments passed when invoking the skill |
| `$ARGUMENTS[N]` / `$N` | Positional argument by 0-based index |
| `$name` | Named argument declared in `arguments` frontmatter list |
| `${CLAUDE_SESSION_ID}` | Current session ID |
| `${CLAUDE_EFFORT}` | Current effort level |
| `${CLAUDE_SKILL_DIR}` | Directory containing the skill's SKILL.md |

## Dynamic Context Injection

Shell commands executed before content reaches Claude. Output replaces the placeholder.

**Inline form:** `` !`git diff HEAD` ``

**Block form:**
````markdown
```!
node --version
npm --version
git status --short
```
````

Use for: injecting live data (diffs, env info, query results) into the skill prompt
without Claude needing a tool call.

Constraints:
- `!` must appear at line start or after whitespace to be recognized
- Substitution runs once; output is not re-scanned for further placeholders
- Disable with `"disableSkillShellExecution": true` in settings

## Context Management

### Inline Execution (default)

Skill content is added to the current conversation. Content persists for the
session and counts against context budget. On auto-compaction, Claude Code
retains the first 5,000 tokens of each invoked skill (up to 25,000 tokens
total across all skills; oldest skills may be dropped). Use for reference
knowledge, guidelines, and lightweight tasks.

### Isolated Execution (`context: fork`)

Skill runs in a subagent with its own context window. No access to conversation
history. Use for self-contained tasks with explicit instructions.

```yaml
---
context: fork
agent: Explore
---
```

The `agent` field selects the execution environment:

| Agent | Tools | Model | Loads CLAUDE.md |
|-------|-------|-------|-----------------|
| `Explore` | Read-only | Haiku | No |
| `Plan` | Read-only | Inherited | No |
| `general-purpose` (default) | All | Inherited | Yes |
| Custom name | Per definition | Per definition | Yes |

Custom agents match `.claude/agents/<name>.md` definitions.

### Choosing Inline vs Fork

| Scenario | Recommended |
|----------|-------------|
| Guidelines, conventions, style rules | Inline |
| Self-contained research or analysis | `context: fork` + `Explore` |
| Code generation with full tool access | `context: fork` + `general-purpose` |
| Tasks needing restricted tools | `context: fork` + custom agent |
| Side-effect actions (deploy, send) | Inline + `disable-model-invocation: true` |

### Relationship to SubAgent Delegation Protocol

The existing `subagent-protocol.md` defines a custom delegation pattern
(role/rules/context/task/constraints sections with runtime variables).
This works on any agent platform.

Claude Code's native `context: fork` provides platform-level isolation without
the custom protocol. Choose based on:

| Criterion | Custom protocol (`subagent-protocol.md`) | Native fork (`context: fork`) |
|-----------|-------------------------------------------|-------------------------------|
| Portability | Any agent platform | Claude Code only |
| Setup | Structured prompt templates per task type | Single `context: fork` field |
| Multi-task workflows | Yes (parallel chunk-brief + render) | Manual orchestration |
| Platform features | Limited | Hooks, memory, MCP, effort |

Both can coexist in a single skill.

## Hooks in Skills

Skills can define lifecycle hooks scoped to their execution:

```yaml
---
hooks:
  PreToolUse:
    - matcher: "Bash"
      hooks:
        - type: command
          command: "./scripts/validate.sh"
  PostToolUse:
    - matcher: "Edit|Write"
      hooks:
        - type: command
          command: "./scripts/lint.sh"
---
```

Use for: input validation, output post-processing, conditional guards on tool use.

## SubAgent Integration

### Preloading Skills into SubAgents

Custom subagent definitions (`.claude/agents/`) can preload skill content
at startup via the `skills` field:

```yaml
---
name: api-developer
description: Implement API endpoints following team conventions
skills:
  - api-conventions
  - error-handling-patterns
---
```

Full skill content is injected into the subagent's context. Skills with
`disable-model-invocation: true` cannot be preloaded.

### Skill-Driven vs SubAgent-Driven

| Approach | Prompt source | Execution control |
|----------|--------------|-------------------|
| Skill with `context: fork` | SKILL.md content | `agent` field |
| SubAgent with `skills` | SubAgent markdown body | SubAgent definition |

Use skill-driven when the task is defined by the skill. Use subagent-driven
when the subagent has its own workflow and skills provide supplementary knowledge.

## Design Considerations

1. **Use `${CLAUDE_SKILL_DIR}`** for script paths to ensure portability across
   install locations (personal, project, plugin)
2. **Prefer `context: fork`** for tasks generating large output to keep main
   context clean
3. **Set `disable-model-invocation: true`** for side-effect skills
   (deploy, send, publish)
4. **Use `paths`** to limit activation to relevant file types
5. **Keep inline skills under 5,000 tokens** as they persist in context
6. **Use dynamic injection** (`` !`command` ``) to provide live data without
   requiring tool calls
7. **Bundle scripts** and reference via `${CLAUDE_SKILL_DIR}/scripts/` instead
   of generating code at runtime
