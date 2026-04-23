# ai-agent-toolkit

Claude Code plugins and reusable skills for AI coding agents (Claude Code, Cursor, etc.).

[日本語版 README はこちら](README_ja.md)

## Plugins

Distributed via the Claude Code plugin marketplace.

| Plugin | Description |
|---|---|
| [taskflow](plugins/taskflow/) | Concurrent task progress and context management across Claude Code sessions |
| [rule-inject](plugins/rule-inject/) | Enforce external rule file reading via `PreToolUse` deny, driven by `CLAUDE.md <rules when="..." src="..."/>` tags. Runs on both Claude Code and Cursor. |

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

Both plugins support Cursor through a manual `.claude/` symlink plus `/…:init` workflow. See the per-plugin README for details.

## Skills

Standalone Agent Skills that can be dropped into any agent without a plugin.

| Skill | Description |
|---|---|
| [create-skill](skills/create-skill/) | Guides through creating effective Agent Skills with best practices, structure templates, and validation checklists |
| [compact-document](skills/compact-document/) | Multi-mode document compaction framework — condenses articles, specs, transcripts, and more with minimal information loss |

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
│   └── compact-document/      Skill: document compaction
├── LICENSE
├── README.md
└── README_ja.md
```

## Contributing

Contributions are welcome. Please open an issue or pull request.

## License

[MIT](LICENSE)
