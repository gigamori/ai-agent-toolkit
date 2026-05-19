# ai-agent-toolkit

Claude Code plugins and reusable skills for AI coding agents (Claude Code, Cursor, etc.).

[日本語版 README はこちら](README_ja.md)

## Plugins

Distributed via the Claude Code plugin marketplace.

| Plugin | Compat | Description |
|---|---|---|
| [taskflow](plugins/taskflow/) | CC | Concurrent task progress and context management across Claude Code sessions |
| [rule-inject](plugins/rule-inject/) | CC / Cursor | Enforce external rule file reading via `PreToolUse` deny, driven by `CLAUDE.md <rules when="..." src="..."/>` tags |

### Installation

Register this repository as a marketplace once:

```
/plugin marketplace add gigamori/ai-agent-toolkit
```

Then install the plugins you want:

```
/plugin install taskflow@ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
```

Each plugin has its own `README.md` with setup, usage, and Cursor compatibility notes.

### Cursor users

Plugins and skills marked **CC / Cursor** support Cursor through a manual `.claude/` symlink plus `/…:init` workflow. Items marked **CC** depend on Claude Code-specific features (hooks, session JSONL, subagents) and are not available in Cursor. See the per-plugin README for details.

## Skills

Standalone Agent Skills that can be dropped into any agent without a plugin.

| Skill | Compat | Description |
|---|---|---|
| [create-skill](skills/create-skill/) | CC / Cursor | Guides through creating effective Agent Skills with best practices, structure templates, and validation checklists |
| [compact-document](skills/compact-document/) | CC / Cursor | Multi-mode document compaction framework — condenses articles, specs, transcripts, and more with minimal information loss |
| [register-pi-tools](skills/register-pi-tools/) | CC / Cursor | Migrates Python scripts to YAML-frontmatter `args` (JSON Schema) + `_tool.args()` runtime, then builds a `tools.yaml` registry consumable by pi or any Anthropic-API tool caller |
| [revert](skills/revert/) | CC | Safely undo recent assistant actions using state-revert semantics — delegates judgment to a bias-isolated subagent to prevent over-removal |

### revert

When an AI assistant makes an unwanted edit, commit, or git operation, the typical response is "undo that" — but the assistant often over-corrects, wiping out an entire session's work instead of just the last change. The **revert** skill solves this by enforcing a two-layer safety protocol:

1. **GATE enforcement** — the main agent is forbidden from deciding what to undo or how. Every revert request must pass through a dedicated `revert-judge` subagent running in a fresh context (bias isolation).
2. **State-revert semantics** — only the recording layer (commit ref, branch pointer, etc.) is removed; the underlying content is preserved. Operations that would also destroy content (scope B) or add inverse operations (scope C) are automatically escalated to the user for confirmation.

**Trigger**: say `戻して` / `undo` / `revert` in conversation, or invoke explicitly with `/revert <target>`.

**Turn-scope default**: vague requests like "undo that" target only the **latest turn**. The skill never silently expands to session-wide changes — if ambiguity exists, it asks first.

**Requirements**: Python 3.11+, [uv](https://docs.astral.sh/uv/), DuckDB (installed automatically by uv).

Copy a skill into your agent's skill folder:

```bash
# Claude Code
cp -r skills/create-skill ~/.claude/skills/

# Cursor
cp -r skills/create-skill ~/.cursor/skills/
```

Or clone and symlink:

```bash
git clone https://github.com/gigamori/ai-agent-toolkit.git
ln -s "$(pwd)/ai-agent-toolkit/skills/create-skill" ~/.claude/skills/create-skill
```

## Structure

```
ai-agent-toolkit/
├── .claude-plugin/
│   └── marketplace.json       Marketplace manifest
├── plugins/
│   ├── taskflow/              Plugin: task progress / context management
│   └── rule-inject/             Plugin: CLAUDE.md rule enforcement
├── skills/
│   ├── create-skill/          Skill: author new skills
│   ├── compact-document/      Skill: document compaction
│   ├── register-pi-tools/     Skill: migrate Python scripts and build tools.yaml
│   └── revert/                Skill: safe undo with bias-isolated judgment
├── LICENSE
├── README.md
└── README_ja.md
```

## Contributing

Contributions are welcome. Please open an issue or pull request.

## License

[MIT](LICENSE)
