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
| [llm-wiki](plugins/llm-wiki/) | CC | Maintain an LLM-curated wiki: ingest sources into Markdown pages, answer questions grounded in them, and lint/promote/view the graph — under hard code gates (write allowlist + single file-journal transaction with rollback; no git involved) |

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

Skills whose documentation goes beyond their `SKILL.md` have non-runtime docs
under [`docs/skills/`](docs/skills/index.md) — user guides and authoring
contracts, indexed there and linked per skill in the **Docs** column below.

| Skill | Compat | Docs | Description |
|---|---|---|---|
| [create-skill](skills/create-skill/) | CC / Cursor | — | Guides through creating effective Agent Skills — best practices, structure templates, validation checklists, and a `validate_frontmatter.py` script — with bundled deep-dive references for advanced authoring, subagent protocols, patterns/examples, debugging, and security |
| [compact-document](skills/compact-document/) | CC / Cursor | [docs](docs/skills/compact-document/) | Multi-mode compaction framework (7 document types, automatic mode detection) — condenses articles, specs, transcripts, and more with minimal information loss; long documents run through a chunked map-reduce pipeline of parallel chunk-brief → merge → render subagents |
| [compact](skills/compact/) | CC | — | Single-pass document compactor invoked as `/compact <text>` — shortens, reorganizes, and deduplicates in one turn with no confirmation gate, inheriting the source's own section skeleton instead of re-templating it into a generic summary. Takes an explicit `document_type` (8 types) and `compression_level` (light/standard/aggressive), enforces rewrite fidelity for numbers, dates, modality, and negation scope, and treats the source as data rather than as instructions. Shadows the built-in `/compact` command |
| [register-pi-tools](skills/register-pi-tools/) | CC / Cursor | [docs](docs/skills/register-pi-tools/) | Migrates Python scripts to YAML-frontmatter `args` (JSON Schema) + `_tool.args()` runtime, then builds a `tools.yaml` registry consumable by pi or any Anthropic-API tool caller; ships a standalone EN/JA user guide and a `build_tools_yaml.py` builder |
| [revert](skills/revert/) | CC | — | Safely undo recent assistant actions using state-revert semantics — delegates judgment to a bias-isolated subagent to prevent over-removal |
| [debug-isolate](skills/debug-isolate/) | CC | — | Isolate iterative debugging in a forked subagent — preserves working tree state with git stash checkpoints and automatic rollback on consecutive failures |
| [run-sql](skills/run-sql/) | CC | — | Execute SQL against configured databases (PostgreSQL, MySQL, MariaDB, Redshift, Snowflake, BigQuery, DuckDB, Databricks) and relay raw JSON results |
| [generate-debug-handoff](skills/generate-debug-handoff/) | CC | — | Generate an E2E-test debug handoff Markdown; requires a `debugger:` arg (human/llm) selecting whether the LLM only formats the table (human approves) or acts as the debugger (no approval) |
| [session-handoff](skills/session-handoff/) | CC | — | Write a handoff document for another LLM session to pick up. The destination is decided solely by the taskflow `current_project` of the turn: `_projects/<project>/llm-handoff/` when a project is assigned, otherwise `_handoffs/llm/` directly under the session's cwd — never under `project-notes/`, since the file is a transient transport document. What to put in the handoff is left to per-case judgment |
| [mode-orchestrator](skills/mode-orchestrator/) | CC / Pi | [docs](docs/skills/mode-orchestrator/) | Read a document holding a todolist + context, then run each step as an isolated `general-purpose` subagent turn with a role-mode `mode:`/`role:` header — one mode (and optional role) per turn, never mixed; autonomous modes only. Resolves each autonomous turn's effort through a bundled human-authored model profile, a fixed per-turn status-line reply contract (a permission denial stops the run instead of entering recovery; a status line that is missing — or present but not on the reply's last line — makes the turn `aborted` and re-run), a background watchdog that bounds each turn in wall-clock time and detects a subagent that stops generating, a bounded `failed`→`debug`→re-execute recovery loop, a bounded decision loop for a turn that reports a fork it may not resolve alone (adjudicated by you by default, or by an inserted llm turn with `--decider=llm` — capped, and escalated back to you for irreversible or outward-facing forks), and per-task-type workflow specs (ships with `dev`) |
| [inspect-cc-log](skills/inspect-cc-log/) | CC | — | Investigate past Claude Code session logs with SQL over pre-built DuckDB views (conversation, tool calls with arguments, file changes, forks, compaction, per-session aggregates) — reconstruct a session, audit tool/subagent calls, trace a file's change history, or bundle a fork tree via a self-contained query script |
| [compact-cc-log](skills/compact-cc-log/) | CC | — | Summarize a past or the current Claude Code session — resolves the session by title, `--session-id`, or `--current` (the running session, cutting the invoking turn out via `scripts/transcript.py`), then delegates to `compact-document` for compression |
| [inspect-pi-log](skills/inspect-pi-log/) | CC | — | Investigate past Pi Coding Agent session logs with SQL over pre-built DuckDB views (conversation, tool calls, file changes, session lineage/bundles, compaction, in-file branches, per-session aggregates) — reconstruct a session, audit tool/subagent calls, trace a file's change history, or bundle a subagent/skill-fork/handoff/fork tree via a self-contained query script |
| [render-report](skills/render-report/) | CC | [docs](docs/skills/render-report/) | Convert a finished analysis report Markdown into pptx / docx / xlsx through the `officecli` CLI without ever modifying the report itself. `docx` / `xlsx` are verbatim transcription. `pptx` is built by an isolated deck subagent that follows the `officecli-pptx` skill and may rewrite for the medium — the layout trial-and-error, screenshots included, stays inside that subagent, so the caller's context never fills with images. Fidelity is checked afterwards, on the finished deck: `check_pptx_text.py` asserts that every claim reached a slide alongside its confidence mark and lists any number that is not in the report, a fresh-context LLM judge checks that nothing was overstated or lost its denominator, and a human reads the contact sheet for readability. An optional scope narrows which findings the deck is built from. Takes no analysis context, so any pipeline emitting contract-conforming Markdown can reuse it |
| [xml-wf](skills/xml-wf/) | CC | [docs](docs/skills/xml-wf/) | Build, run, and resume XML v2 workflows: a task is decomposed into single-responsibility steps orchestrated deterministically by a bundled Python runner (`wfrun`) — not by an LLM. Batch execution has two backends selected by `--backend` (auto-detected from the harness): run-cc runs each step as an isolated `claude -p` subagent, run-pi runs it on the pi CLI with no claude install at all, refusing `schema=` and `on-error="debug"` up front because pi cannot enforce them. The run-llm protocol instead delegates steps through the hosting agent's own subagent facility. A step that hits a genuinely ambiguous fork raises a `DECISION:` request instead of silently guessing — the run stops for a human answer, or `decider="llm"` has an adjudicator model settle it unattended on either backend |

### revert

When an AI assistant makes an unwanted edit, commit, or git operation, the typical response is "undo that" — but the assistant often over-corrects, wiping out an entire session's work instead of just the last change. The **revert** skill solves this by enforcing a two-layer safety protocol:

1. **GATE enforcement** — the main agent is forbidden from deciding what to undo or how. Every revert request must pass through a dedicated `revert-judge` subagent running in a fresh context (bias isolation).
2. **State-revert semantics** — only the recording layer (commit ref, branch pointer, etc.) is removed; the underlying content is preserved. Operations that would also destroy content (scope B) or add inverse operations (scope C) are automatically escalated to the user for confirmation.

**Trigger**: say `戻して` / `undo` / `revert` in conversation, or invoke explicitly with `/revert <target>`.

**Turn-scope default**: vague requests like "undo that" target only the **latest turn**. The skill never silently expands to session-wide changes — if ambiguity exists, it asks first.

**Requirements**: Python 3.11+, [uv](https://docs.astral.sh/uv/), DuckDB (installed automatically by uv).

### render-report

Turning a finished analysis into slides tends to corrupt it: whoever lays out the deck also starts editing the wording, and a number quietly loses its denominator on the way into a text box. Forbidding the rewriting outright does not fix it either — a faithful transcription of a long report is a deck nobody reads, and the pressure to shorten lands on the analysis instead. **render-report** lets a deck skill do the rewriting, contains it, and checks the result:

1. **Contract gate** — `scripts/validate_contract.py` checks the input Markdown against a structure contract before anything is converted: frontmatter `purpose`, a fixed section order, claim blocks that must carry a sample size and a confidence mark, and chart annotations whose `x=` / `y=` name real columns of the table that follows. A violation is returned as a list of offending lines instead of a half-correct deck.
2. **A deck subagent that owns the design** — for `pptx`, the deck is built by an isolated subagent following the `officecli-pptx` skill, which is the standard for what a good deck looks like. It decides the structure, the slide count and what to leave out. Its screenshot-and-fix loop, including a second subagent that judges the rendered slides adversarially, runs entirely inside it; the caller gets back a contact-sheet path and a short declaration, never images.
3. **Deterministic gates on the finished deck** — `scripts/check_pptx_text.py` asserts that every in-scope claim reached a slide together with its confidence mark, and separately *lists* every number on the deck that is not in the report. It lists rather than fails because no predicate separates an invented statistic from a decorative section counter — the judge makes that call. `officecli validate` and `view issues` are re-run by the caller rather than trusted, and explicit colours are checked against the WCAG floor.
4. **A judge and a human** — a fresh-context LLM judge reads the rendered text against the report for the five things a token check cannot see: strengthened modality, a dropped denominator, an invented claim, a swapped emphasis, a contradiction between slides. A human reads the contact sheet last, for readability alone.

The report Markdown is never modified. When text does not fit, the deck subagent resizes, splits or narrows what it carries — the analysis is not touched. An optional **scope** ("only F-6 and F-7") narrows the input the deck is built from; it never specifies a page count.

Because it takes **no analysis context** — only the Markdown and an output path — any pipeline that emits contract-conforming Markdown can reuse it, and the layout work stays out of the analysis context window.

**Formats**: `pptx` goes through the deck subagent described above. `docx` bulk-imports the Markdown through officecli's `markdown` element, so the CLI — not a model — performs the transcription. `xlsx` is a data appendix (one table per sheet) and is produced only on explicit request.

**Trigger**: ask to convert an analysis report to slides or a document, or invoke `/render-report <md-path> <format>`.

**Requirements**: [officecli](https://d.officecli.ai/), Python 3.11+, [uv](https://docs.astral.sh/uv/).

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
│   ├── session-handoff/        Skill: write a handoff for another LLM session
│   ├── mode-orchestrator/      Skill: run a todolist through role-mode subagent turns
│   ├── inspect-cc-log/         Skill: SQL views over CC logs for session investigation
│   ├── compact-cc-log/         Skill: summarize a past or the current CC session
│   ├── inspect-pi-log/         Skill: SQL views over Pi Coding Agent logs for session investigation
│   ├── render-report/          Skill: render a contract-conforming report Markdown to pptx/docx/xlsx
│   └── xml-wf/                 Skill: deterministic XML v2 workflow runner (wfrun)
├── docs/
│   ├── skills/                Non-runtime skill docs (user guides, authoring contracts)
│   │   ├── index.md           Index of the skill doc dirs below
│   │   ├── xml-wf/            xml-wf reference README + user guide (EN/JA)
│   │   ├── register-pi-tools/ register-pi-tools user guide (EN/JA)
│   │   ├── mode-orchestrator/ mode-orchestrator user guide (EN/JA) + workflow-spec authoring
│   │   └── compact-document/  compact-document authoring contract
│   └── dev/                   Repo-level developer docs (owned by no single skill)
│       ├── index.md           Index of the dev docs below
│       ├── test-gate.md       The test-knowledge gate over skills/ and its migration record
│       └── test-constraints.md Outside-world facts the skills/ test suites depend on
├── tests/                     Repo-wide checks owned by no plugin or skill
│   ├── lint_test_knowledge.py Pre-test gate: no comments/docstrings, no unreachable refs
│   └── .knowledge-allowlist   Violations frozen when that gate was introduced
├── LICENSE
├── README.md
└── README_ja.md
```

## License

[MIT](LICENSE)
