# ai-agent-toolkit

Reusable skills, hooks, and harness components for AI coding agents (Claude Code, Cursor, etc.).

[日本語版 README はこちら](README_ja.md)

## Overview

A collection of portable building blocks that extend AI coding agents with specialized capabilities. Each component is designed to work across multiple agent platforms.

## Skills

| Skill | Description |
|-------|-------------|
| [create-skill](skills/create-skill/) | Guides through creating effective Agent Skills with best practices, structure templates, and validation checklists |
| [compact-document](skills/compact-document/) | Multi-mode document compaction framework — condenses articles, specs, transcripts, and more with minimal information loss |

## Installation

Copy skill directories into your agent's skill folder:

```bash
# Claude Code
cp -r skills/create-skill ~/.claude/skills/

# Cursor
cp -r skills/create-skill ~/.cursor/skills/
```

Or clone the repository and symlink:

```bash
git clone https://github.com/gigamori/ai-agent-toolkit.git
ln -s "$(pwd)/ai-agent-toolkit/skills/create-skill" ~/.claude/skills/create-skill
```

## Structure

```
ai-agent-toolkit/
├── skills/
│   ├── create-skill/       # Skill authoring guide
│   └── compact-document/   # Document compaction
├── LICENSE
├── README.md
└── README_ja.md
```

## Contributing

Contributions are welcome. Please open an issue or pull request.

## License

[MIT](LICENSE)
