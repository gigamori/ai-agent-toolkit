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
| `disallowed-tools` | Tools removed from the skill's available pool |
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

Works in `context: fork` as well as inline: the shell runs at render time and the
output is placed into the fork's prompt.

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

The skill body runs as the prompt of an isolated subagent with its own context
window. It does not inherit the parent conversation history — the skill body
(plus substituted arguments and injected dynamic context) is the only task
context the subagent receives. The fork's own work (drafts, tool output) does
not return to the parent; only the final summary does.

```yaml
---
context: fork
agent: Explore
---
```

The `agent` field selects the execution environment:

| Agent | Tools | Model |
|-------|-------|-------|
| `Explore` | Read-only | Haiku |
| `Plan` | Read-only | Inherited |
| `general-purpose` (default) | All | Inherited |
| Custom name | Per definition | Per definition |

Custom agents match `.claude/agents/<name>.md` definitions.

### Passing Context to a Fork

`context: fork` never inherits conversation history. To supply context, use:

- `$ARGUMENTS` / `$N` / `$name` substitution (declared in `arguments`)
- Dynamic injection `` !`command` ``
- A serialized handoff document for cross-session transfer

Only typed arguments and a handoff document can convey the actual discussion;
dynamic injection conveys command/environment output, not the conversation.

If a forked execution must see the conversation, do NOT use `context: fork`
(it never inherits history). Instead keep the skill inline and have its body
delegate generation to the Agent tool with `subagent_type:"fork"`: that fork
inherits the full conversation and the inline skill body. Delegation to
`subagent_type:"fork"` can silently return a contextless general-purpose result
with no error — validate the returned result; do not assume inheritance succeeded.

### Choosing Inline vs Fork

| Scenario | Recommended |
|----------|-------------|
| Guidelines, conventions, style rules | Inline |
| Self-contained research or analysis | `context: fork` + `Explore` |
| Code generation with full tool access | `context: fork` + `general-purpose` |
| Tasks needing restricted tools | `context: fork` + custom agent / `disallowed-tools` |
| Side-effect actions (deploy, send) | Inline + `disable-model-invocation: true` |

### Model Selection

The `model` field (and a custom `agent`'s model) sets the capability tier a forked
skill runs at. Match it to how much instruction-adherence the body demands.

- **Avoid primitive / small models for procedural bodies.** A body that says "always
  run this command and branch only on its output, never judge the input" needs a
  model that follows imperatives literally. A primitive model (e.g. `haiku`) may
  short-circuit — refusing to execute for inputs it pattern-matches as implausible
  or placeholder, and returning an empty/error result *without running anything*,
  even ignoring an explicit "run this first" step. **Default to `sonnet` or stronger
  for such skills**; reserve the smallest tier for trivial, judgment-free text shaping.
- **Symptom vs. cause.** If a forked skill returns "no input" / empty-sentinel for a
  valid request, confirm argument delivery before blaming the model: add a temporary
  `echo "[$ARGUMENTS]"` line. If the value is present yet the command never ran, the
  cause is the model skipping execution — raise the tier.
- **Porting a `model: fast` subagent?** `fast` denotes a *capable* fast tier, not the
  smallest model, and is not a valid skill `model` value. Substitute `sonnet` or
  `inherit` — not `haiku`.
- **Iteration note.** A skill's body and frontmatter (including `model`) are re-read
  on each invocation, so model/body edits take effect immediately; only newly added
  skills need a reload to be discovered.

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

Hooks defined in skill frontmatter fire during inline execution. They do not
fire for tool calls made inside a `context: fork` subagent.

## SubAgent Integration

A skill can involve subagents in four ways:

| Route | Skill-side declaration | What the subagent sees |
|-------|------------------------|------------------------|
| Skill runs *as* a subagent | `context: fork` (+ `agent`) | Skill body + arguments + dynamic injection (no conversation history) |
| Skill body *delegates* to a non-fork agent | A delegation prompt in the body (`subagent-protocol.md` pattern) | That delegation prompt only (no conversation history) |
| Inline skill *delegates* to a fork | Agent tool call with `subagent_type:"fork"` in the body | Full conversation history + the inline-injected skill body |
| Custom subagent *preloads* the skill | Agent-side `skills:` field | Full skill body injected into the agent's context |

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
| Platform features | Limited | Hooks, MCP, effort |

Both can coexist in a single skill.

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
3. **Pass conversation context explicitly to forked skills** — via `arguments`
   substitution, dynamic injection (`` !`command` ``), or a serialized handoff
   document. `context: fork` does not inherit conversation history.
4. **Set `disable-model-invocation: true`** for side-effect skills
   (deploy, send, publish)
5. **Use `paths`** to limit activation to relevant file types
6. **Keep inline skills under 5,000 tokens** as they persist in context
7. **Use dynamic injection** (`` !`command` ``) to provide live data without
   requiring tool calls
8. **Bundle scripts** and reference via `${CLAUDE_SKILL_DIR}/scripts/` instead
   of generating code at runtime
