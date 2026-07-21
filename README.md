# ai-agent-toolkit

Claude Code plugins and reusable skills for AI coding agents (Claude Code, Cursor, etc.).

[日本語版 README はこちら](README_ja.md)

## Plugins

Distributed via the Claude Code plugin marketplace.

| Plugin | Compat | Description |
|---|---|---|
| [taskflow](plugins/taskflow/) | CC | Concurrent task progress and context management across Claude Code sessions |
| [rule-inject](plugins/rule-inject/) | CC / Cursor | Enforce external rule file reading via `PreToolUse` deny, driven by `CLAUDE.md <rules when="..." src="..."/>` tags |
| [role-mode](plugins/role-mode/) | CC | Declare a cognitive `mode:` and/or `role:` per turn via prompt slugs; injects the matching NEVER/DO rules and framework meta through `UserPromptSubmit` (nothing injected without a slug) |
| [llm-wiki](plugins/llm-wiki/) | CC | Maintain an LLM-curated wiki: ingest sources into Markdown pages, answer questions grounded in them, and lint/promote/view the graph — under hard code gates (write allowlist + single git transaction with rollback) |

### Installation

Register this repository as a marketplace once:

```
/plugin marketplace add gigamori/ai-agent-toolkit
```

Then install the plugins you want:

```
/plugin install taskflow@ai-agent-toolkit
/plugin install rule-inject@ai-agent-toolkit
/plugin install llm-wiki@ai-agent-toolkit
```

Each plugin has its own `README.md` with setup, usage, and Cursor compatibility notes.

### Cursor users

Plugins and skills marked **CC / Cursor** support Cursor through a manual `.claude/` symlink plus `/…:init` workflow. Items marked **CC** depend on Claude Code-specific features (hooks, session JSONL, subagents) and are not available in Cursor. See the per-plugin README for details.

## Skills

Standalone Agent Skills that can be dropped into any agent without a plugin.

| Skill | Compat | Docs | Description |
|---|---|---|---|
| [create-skill](skills/create-skill/) | CC / Cursor | — | Guides through creating effective Agent Skills — best practices, structure templates, validation checklists, and a `validate_frontmatter.py` script — with bundled deep-dive references for advanced authoring, subagent protocols, patterns/examples, debugging, and security |
| [compact-document](skills/compact-document/) | CC / Cursor | — | Multi-mode compaction framework (7 document types, automatic mode detection) — condenses articles, specs, transcripts, and more with minimal information loss; long documents run through a chunked map-reduce pipeline of parallel chunk-brief → merge → render subagents |
| [compact](skills/compact/) | CC | — | Single-pass document compactor invoked as `/compact <text>` — shortens, reorganizes, and deduplicates in one turn with no confirmation gate, inheriting the source's own section skeleton instead of re-templating it into a generic summary. Takes an explicit `document_type` (8 types) and `compression_level` (light/standard/aggressive), enforces rewrite fidelity for numbers, dates, modality, and negation scope, and treats the source as data rather than as instructions. Shadows the built-in `/compact` command |
| [register-pi-tools](skills/register-pi-tools/) | CC / Cursor | [docs](docs/skills/register-pi-tools/) | Migrates Python scripts to YAML-frontmatter `args` (JSON Schema) + `_tool.args()` runtime, then builds a `tools.yaml` registry consumable by pi or any Anthropic-API tool caller; ships a standalone EN/JA user guide and a `build_tools_yaml.py` builder |
| [revert](skills/revert/) | CC | — | Safely undo recent assistant actions using state-revert semantics — delegates judgment to a bias-isolated subagent to prevent over-removal |
| [debug-isolate](skills/debug-isolate/) | CC | — | Isolate iterative debugging in a forked subagent — preserves working tree state with git stash checkpoints and automatic rollback on consecutive failures |
| [run-sql](skills/run-sql/) | CC | — | Execute SQL against configured databases (PostgreSQL, MySQL, MariaDB, Redshift, Snowflake, BigQuery, DuckDB, Databricks) and relay raw JSON results |
| [generate-debug-handoff](skills/generate-debug-handoff/) | CC | — | Generate an E2E-test debug handoff Markdown; requires a `debugger:` arg (human/llm) selecting whether the LLM only formats the table (human approves) or acts as the debugger (no approval) |
| [mode-orchestrator](skills/mode-orchestrator/) | CC | — | Read a document holding a todolist + context, then run each step as an isolated `general-purpose` subagent turn with a role-mode `mode:`/`role:` header — one mode (and optional role) per turn, never mixed; autonomous modes only |
| [inspect-cc-log](skills/inspect-cc-log/) | CC | — | Investigate past Claude Code session logs with SQL over pre-built DuckDB views (conversation, tool calls with arguments, file changes, forks, compaction, per-session aggregates) — reconstruct a session, audit tool/subagent calls, trace a file's change history, or bundle a fork tree via a self-contained query script |
| [inspect-pi-log](skills/inspect-pi-log/) | CC | — | Investigate past Pi Coding Agent session logs with SQL over pre-built DuckDB views (conversation, tool calls, file changes, session lineage/bundles, compaction, in-file branches, per-session aggregates) — reconstruct a session, audit tool/subagent calls, trace a file's change history, or bundle a subagent/skill-fork/handoff/fork tree via a self-contained query script |
| [xml-wf](skills/xml-wf/) | CC | [docs](docs/skills/xml-wf/) | Build, run, and resume XML v2 workflows: a task is decomposed into single-responsibility steps, each executed as an isolated `claude -p` subagent under an explicit role and mode, orchestrated deterministically by a bundled Python runner (`wfrun`) — not by an LLM |

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
│   ├── rule-inject/           Plugin: CLAUDE.md rule enforcement
│   ├── role-mode/             Plugin: per-turn cognitive mode / role injection
│   └── llm-wiki/              Plugin: LLM-curated wiki (ingest / query / lint / promote / view)
├── skills/
│   ├── create-skill/          Skill: author new skills
│   ├── compact-document/      Skill: document compaction
│   ├── compact/               Skill: single-pass document compaction (/compact)
│   ├── register-pi-tools/     Skill: migrate Python scripts and build tools.yaml
│   ├── revert/                Skill: safe undo with bias-isolated judgment
│   ├── debug-isolate/         Skill: isolated debugging with forked subagent
│   ├── run-sql/               Skill: run SQL against configured databases
│   ├── generate-debug-handoff/ Skill: generate E2E debug handoff Markdown
│   ├── mode-orchestrator/      Skill: run a todolist through role-mode subagent turns
│   ├── inspect-cc-log/         Skill: SQL views over CC logs for session investigation
│   ├── inspect-pi-log/         Skill: SQL views over Pi Coding Agent logs for session investigation
│   └── xml-wf/                 Skill: deterministic XML v2 workflow runner (wfrun)
├── docs/
│   └── skills/                Non-runtime skill docs (user guides, authoring contracts)
│       ├── xml-wf/            xml-wf reference README + user guide (EN/JA)
│       ├── register-pi-tools/ register-pi-tools user guide (EN/JA)
│       └── compact-document/  compact-document authoring contract
├── LICENSE
├── README.md
└── README_ja.md
```

## License

[MIT](LICENSE)
