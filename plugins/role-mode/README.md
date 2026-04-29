# role-mode

A Claude Code plugin that lets the user declare a **cognitive mode** and/or a **role** for each turn via `mode:<name>` and `role:<value>` slugs in the prompt. When at least one slug is present, the framework meta (Two response axes / Mode > Role) plus the active Role/Mode declaration plus the matching mode rules and common rules are injected through a `UserPromptSubmit` hook. When no slug is present, **nothing is injected** and the LLM behaves exactly as it would without the plugin.

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
role:<value>
role:"<value>"
```

Both `mode:` and `role:` are optional. The hook fires when at least one is present. The first occurrence per kind is consumed; subsequent occurrences are ignored. Both slugs can appear at any position (start of input or after whitespace), mirroring `pj:` from the taskflow plugin. Prefix matching is case-insensitive.

#### `mode:<name>`

- `<name>` matches `[A-Za-z][A-Za-z0-9_-]*`. Captured value is normalized to lowercase.
- Unknown mode names are silently ignored — no failure, no warning.
- Mode aliases: `verify` resolves to `debug`, `implement` resolves to `execute`. Your chosen alias is preserved in the displayed `mode:` line; only the underlying rules file is shared.

#### `role:<value>`

- `<value>` is free-form (multibyte and spaces allowed). The value is preserved verbatim — no case folding.
- Three terminator rules, in precedence order:
  - **Quoted** `role:"<value>"` — captures everything between the double quotes verbatim. Use this when the value contains literal `mode:` / `pj:` or other tokens that would otherwise terminate the unquoted form.
  - **Unquoted, next slug** — capture stops at the next ` mode:` or ` pj:` slug.
  - **Unquoted, end of line / input** — capture stops at the next newline or end of input.
- Empty quoted value (`role:""`) is treated as no role.

Example prompts:

```
mode:plan design the migration steps for the auth refactor
pj:harness-modes mode:execute apply the scaffold from the design note
mode:debug role:厳格なコードレビュアー、セキュリティ重視で挙動を批判的に検証する
このコードを見て
role:"senior backend engineer" mode:debug investigate this race condition
explore the repo first, then mode:survey list the open API contracts
```

### Escape tokens

To write a slug literally without triggering injection, add the corresponding escape token anywhere in the prompt:

| Token | Effect |
|---|---|
| `nomode` | Skip `mode:` detection for this turn |
| `norole` | Skip `role:` detection for this turn |

Both tokens can be combined. Example: discussing the plugin itself without activating any mode:

```
nomode norole — how does mode:plan differ from mode:execute?
```

### Available modes

| Mode | Aliases | Use it when... |
|---|---|---|
| `mode:ask` | | You want a direct, grounded answer to a single question |
| `mode:discuss` | | You want an expert opinion or a decision-driving conversation |
| `mode:brainstorm` | | You want diverse ideas without judgment or evidence pressure |
| `mode:organize` | | You want help structuring fragmented thoughts |
| `mode:survey` | | You want fact collection — no solutions, no proposals |
| `mode:plan` | | You want actionable steps with clear criteria, no final deliverables |
| `mode:execute` | `implement` | You want the plan applied strictly, no scope expansion |
| `mode:debug` | `verify` | You want root-cause analysis, no premature fixes |
| `mode:review` | | You want process evaluation and lessons learned |

The full `NEVER` / `DO` rules for each mode live in [`prompts/modes/`](prompts/modes/).

### What gets injected

| Slugs present | Injected blocks |
|---|---|
| `mode:` only | `_meta.md` + `mode: <name>` + mode rules + `_common.md` |
| `role:` only | `_meta.md` + `role: <value>` |
| Both | `_meta.md` + `role: <value>` + `mode: <name>` + mode rules + `_common.md` |
| Neither | nothing (hook exits silently) |

`_common.md` (mode-only rules):

```markdown
- NEVER: overstep(mode boundary), change-mode-silently
- DO: declare(current mode), report(transition needs), cite(every claim except for brainstorming)

Answer starting with `[Mode: current_mode]`
```

`_meta.md` framework header (always paired with any active slug; includes the `[BLOCKED: mode-rule <name>]` self-report rule).

### Behavior without a slug

If neither `mode:` nor `role:` appears in the prompt, the hook exits without emitting any context. The LLM sees exactly the same input it would see without the plugin installed. **Zero behavioral change is guaranteed by design.**

## How it works

```
User prompt
  │
  ├─ [UserPromptSubmit hook: mode_inject.py]
  │     ├─ reads stdin (UTF-8 BOM tolerant)
  │     ├─ MODE_RE: (?:^|\s)mode:([A-Za-z][A-Za-z0-9_-]*)         (case-insensitive, lowercased)
  │     ├─ ROLE_RE: (?:^|\s)role:(?:"([^"]*)"|(.+?)(?=...))       (case-insensitive prefix, verbatim value)
  │     ├─ resolve mode alias (verify→debug, implement→execute)
  │     ├─ if neither slug → exit 0, no output
  │     └─ else emit JSON additionalContext = _meta.md + active block + (mode rules + _common.md when mode set)
  │
  └─ LLM receives the prompt plus the injected framework + active declaration
```

### File layout

```
plugins/role-mode/
  .claude-plugin/plugin.json
  hooks/
    hooks.json
    mode_inject.py            # UserPromptSubmit hook
  prompts/modes/
    _meta.md                  # framework header (axes / conflict rule / BLOCKED)
    _common.md                # ALL MODES rules + answer-prefix instruction (mode-only)
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

Each `<mode>.md` contains exactly three lines: `Basic Behavior`, `NEVER`, `DO`. The `mode: <name>` header is generated dynamically by the hook and is not stored in the file.

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
