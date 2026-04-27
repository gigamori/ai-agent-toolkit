# role-mode

A Claude Code plugin that lets the user declare a **cognitive mode** for each turn via a `mode:<name>` slug in the prompt. When the slug is present, the matching mode definition (a tight set of `NEVER` / `DO` rules) plus a small set of common rules are injected into the conversation through a `UserPromptSubmit` hook. When the slug is absent, **nothing is injected** and the LLM behaves exactly as it would without the plugin.

Claude Code only. Cursor is **not supported** because Cursor's `beforeSubmitPrompt` hook can only continue/block submissions and cannot inject context (only `sessionStart` can inject context, and it fires once per conversation rather than per turn — incompatible with slug-based per-turn injection).

[日本語版 README はこちら](README_ja.md)

## What it solves

Generic LLM responses tend to be unscoped — the same prompt can drift between exploring, planning, and executing in a single answer. role-mode lets the user pick one cognitive frame per turn and binds the model to it:

- `mode:survey` → fact collection only, no proposals
- `mode:plan` → structure steps, do not produce final deliverables
- `mode:execute` → follow the plan, no scope expansion
- `mode:debug` → assume broken, find root causes
- ... etc.

The slug is opt-in and per-turn. Without a slug, the plugin is invisible.

## Installation (Claude Code)

### Via the plugin marketplace (recommended)

```
/plugin marketplace add gigamori/ai-agent-toolkit
/plugin install role-mode@ai-agent-toolkit
```

### Local (development / testing)

```bash
claude --plugin-dir ./plugins/role-mode
```

There is no separate `init` step — once the plugin is enabled, the `UserPromptSubmit` hook is active and slugs are detected automatically.

## Usage

### Slug syntax

```
mode:<name>
```

- The first occurrence in the user prompt is consumed; subsequent occurrences are ignored.
- The slug can appear at any position (start of input or after whitespace), mirroring `pj:` from the taskflow plugin.
- Names are lowercase ASCII (`[a-z][a-z0-9_-]*`).
- Unknown mode names are silently ignored — the plugin does not fail or warn.

Example prompts:

```
mode:plan design the migration steps for the auth refactor
pj:harness-modes mode:execute apply the scaffold from the design note
explore the repo first, then mode:survey list the open API contracts
```

### Available modes

| Mode | Use it when... |
|---|---|
| `mode:ask` | You want a direct, grounded answer to a single question |
| `mode:discuss` | You want an expert opinion or a decision-driving conversation |
| `mode:brainstorm` | You want diverse ideas without judgment or evidence pressure |
| `mode:organize` | You want help structuring fragmented thoughts |
| `mode:survey` | You want fact collection — no solutions, no proposals |
| `mode:plan` | You want actionable steps with clear criteria, no final deliverables |
| `mode:execute` | You want the plan applied strictly, no scope expansion |
| `mode:debug` | You want root-cause analysis, no premature fixes |
| `mode:review` | You want process evaluation and lessons learned |

The full `NEVER` / `DO` rules for each mode live in [`prompts/modes/`](prompts/modes/).

### Common rules (always injected with any mode)

```markdown
## ALL MODES
- NEVER: overstep(mode boundary), change-mode-silently
- DO: declare(current mode), report(transition needs), cite(every claim except for brainstorming)

On rule violation, stop and self-report with the marker `[BLOCKED: mode-rule <name>]` before proceeding.
```

### Behavior without a slug

If no `mode:<name>` appears in the prompt, the hook exits without emitting any context. The LLM sees exactly the same input it would see without the plugin installed. **Zero behavioral change is guaranteed by design.**

## How it works

```
User prompt
  │
  ├─ [UserPromptSubmit hook: mode_inject.py]
  │     ├─ reads stdin (UTF-8 BOM tolerant)
  │     ├─ regex search: (?:^|\s)mode:([a-z][a-z0-9_-]*)
  │     ├─ if no match → exit 0, no output
  │     ├─ if match but file missing → exit 0, no output
  │     └─ else emit JSON additionalContext = mode file + _common.md
  │
  └─ LLM receives the prompt plus the injected mode rules
```

### File layout

```
plugins/role-mode/
  .claude-plugin/plugin.json
  hooks/
    hooks.json
    mode_inject.py            # UserPromptSubmit hook
  prompts/modes/
    _common.md                # ALL MODES rules
    ask.md
    discuss.md
    brainstorm.md
    organize.md
    survey.md
    plan.md
    execute.md
    debug.md
    review.md
```

## Interop

### With taskflow

Both plugins use `UserPromptSubmit` hooks on Claude Code. They are independent — taskflow injects project state, role-mode injects mode rules. Hook order is unspecified by Claude Code but the two outputs are concatenated, not merged, so order does not matter for correctness.

The `Plan` mode definition is intentionally compatible with taskflow's `_projects/<project>/` scaffold updates: `NEVER: generate-final-deliverables` permits design / process documents (which is what `progress.md` and `handoff/` updates are).

### With rule-inject

rule-inject hooks are `PreToolUse` / `PostToolUse`, role-mode is `UserPromptSubmit` — no intersection. Both use a `BLOCKED:` self-report convention; role-mode disambiguates with `[BLOCKED: mode-rule <name>]`.

## Roadmap

| Phase | Work | Status |
|---|---|---|
| 0 | Mode files + slug detect + inject hook (minimal) | ✅ Done |
| 1 | README + marketplace registration | ✅ Done |
| 2 | Mode persistence (state-based `last_mode`) | future |
| 3 | Violation detection hook (PostResponse-style) | future |

## License

[MIT](../../LICENSE)
