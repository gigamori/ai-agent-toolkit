---
name: create-skill
description: "Guides users through creating, fixing, and reviewing Agent Skills. Use when the user wants to create, write, or author a new skill; fix, update, or extend an existing skill; or review/audit a skill's structure, frontmatter, or SKILL.md format."
argument-hint: "[--review|--fix] [path-to-skill]"
---
# Creating, Fixing, and Reviewing Agent Skills

This skill guides you through creating, fixing, and reviewing effective Agent Skills. Skills are markdown files that teach the agent how to perform specific tasks: reviewing PRs using team standards, generating commit messages in a preferred format, querying database schemas, or any specialized workflow. Skills work across various AI coding agents and API.

`argument-hint` is a Claude Code-only convenience (see `advanced-mode.md`); mode
detection below works from natural language on any platform, with or without it.

## Mode

Three modes share this skill. Determine which applies before doing anything else:

| Mode | When | What happens |
|------|------|---------------|
| **Create** (default) | No existing skill is referenced, or the user wants a brand-new skill | Full workflow below: Discovery → Design → Implementation → Verification |
| **Review** | User asks to review, audit, or check an existing skill (or invokes with `--review`) | Read-only: evaluate the target against the Summary Checklist, Anti-Patterns, and `security-and-troubleshooting.md`; report findings; do not edit files unless separately asked |
| **Fix** | User asks to fix, update, extend, or rename an existing skill (or invokes with `--fix`) | Read the target skill's existing files, apply only the requested change, then run Phase 4 (Verification) on the result |

Detecting the mode:
- If invoked as a slash command, check `$ARGUMENTS` for `--review` or `--fix`; treat the remaining text as the target skill's path.
- Otherwise infer from the request: "review / audit / check / does this follow best practices" → Review; "fix / update / add to / rename / change" an existing skill → Fix; anything else → Create.
- If a target path is given but has no `SKILL.md`, say so — don't silently fall back to Create mode for a path the user explicitly pointed at.

**Review mode**: skip Phases 1-3 entirely. Go straight to the Phase 4 checks (Summary Checklist, Security & Naming, Structure, Scripts sections) against the existing files and report deviations.

**Fix mode**: skip Phase 1 (Discovery) and Phase 2 (Design) unless the requested change is broad enough to need re-scoping. Go directly to the relevant part of Phase 3 (Implementation) for the specific change, then Phase 4 (Verification).

---

## Create Mode: Before You Begin — Gather Requirements

Before creating a skill, gather essential information from the user about:

1. **Purpose and scope**: What specific task or workflow should this skill help with? Identify 2-3 concrete use cases.
2. **Skill category**: Which type best fits?
   - Document & Asset Creation (consistent output: docs, presentations, code)
   - Workflow Automation (multi-step processes, coordination)
   - MCP Enhancement (workflow guidance on top of MCP tool access)
3. **Target location**: Personal skill (agent's global skill directory) or project skill (repo-local skill directory)?
4. **Trigger scenarios**: When should the agent automatically apply this skill?
5. **Key domain knowledge**: What specialized information does the agent need that it wouldn't already know?
6. **Output format preferences**: Are there specific templates, formats, or styles required?
7. **Existing patterns**: Are there existing examples or conventions to follow?

### Inferring from Context

If you have previous conversation context, infer the skill from what was discussed. You can create skills based on workflows, patterns, or domain knowledge that emerged in the conversation.

### Gathering Additional Information

If you need clarification, use a structured question tool when available (e.g. `AskUserQuestion` in Claude Code — tool names vary by platform):

```
Example structured question usage:
- "Where should this skill be stored?" with options like ["Personal (global)", "Project (repo-local)"]
- "Should this skill include executable scripts?" with options like ["Yes", "No"]
```

If no structured question tool is available, ask these questions conversationally.

---

## Skill File Structure

### Directory Layout

Skills are stored as directories containing a `SKILL.md` file:

```
skill-name/
├── SKILL.md              # Required - main instructions
├── references/           # Optional - documentation
│   ├── api-guide.md
│   └── examples.md
├── scripts/              # Optional - executable code
│   ├── validate.py
│   └── helper.sh
└── assets/               # Optional - templates, fonts, icons
    └── report-template.md
```

### Storage Locations

| Type | Example path | Scope |
|------|-------------|-------|
| Personal | ~/.<agent>/skills/skill-name/ | Available across all your projects |
| Project | .<agent>/skills/skill-name/ | Shared with anyone using the repository |

### SKILL.md Structure

Every skill requires a `SKILL.md` file with YAML frontmatter and markdown body:

```markdown
---
name: your-skill-name
description: Brief description of what this skill does and when to use it
---

# Your Skill Name

## Instructions
Clear, step-by-step guidance for the agent.

## Examples
Concrete examples of using this skill.
```

### Metadata Fields

| Field | Required | Requirements | Purpose |
|-------|----------|--------------|---------|
| `name` | Yes | Max 64 chars, lowercase letters/numbers/hyphens only. Must match folder name. Verb-object form recommended (see Anti-Patterns §5). | Unique identifier |
| `description` | Yes | Max 1024 chars, non-empty, no XML angle brackets (`<` `>`) | Helps agent decide when to apply the skill |
| `license` | No | e.g., MIT, Apache-2.0 | Open-source distribution |
| `allowed-tools` | No | e.g., `"Bash(python:*) WebFetch"` | Restrict tool access |
| `compatibility` | No | 1-500 chars | Environment requirements |
| `metadata` | No | Custom key-value pairs (author, version, mcp-server) | Additional info |

Security restrictions in frontmatter:
- Avoid XML angle brackets (`<` `>`) in frontmatter values. The Agent Skills open standard does not forbid them, but some platforms (e.g. claude.ai skill upload) reject them because frontmatter is injected into the system prompt (injection risk) — safest to omit them everywhere

**Always wrap string values in double quotes** — especially `description`. Frontmatter is parsed as YAML, where an unquoted value breaks the parse when it contains a `:` followed by a space, contains a `#`, or *starts* with an indicator character (`# > | - ! & * ? : [ ] { } @ %`). Quoting neutralizes all of these:

```yaml
# Breaks: the ": " after the first phrase is parsed as a mapping key/value separator
description: Generate a db spec. Supported DB: BigQuery / Snowflake / Redshift

# Works: the whole value is a single quoted string
description: "Generate a db spec. Supported DB: BigQuery / Snowflake / Redshift"
```

Do not place a raw ASCII double quote (`"`) inside a double-quoted value — escape it as `\"`, or avoid it. After writing the frontmatter, validate that it parses (see Phase 4).

---

## Writing Effective Descriptions

The description is **critical** for skill discovery. The agent uses it to decide when to apply your skill.

### Description Best Practices

1. **Write in third person** (the description is injected into the system prompt):
   - ✅ Good: "Processes Excel files and generates reports"
   - ❌ Avoid: "I can help you process Excel files"
   - ❌ Avoid: "You can use this to process Excel files"

2. **Be specific and include trigger terms**:
   - ✅ Good: "Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction."
   - ❌ Vague: "Helps with documents"

3. **Include both WHAT and WHEN**:
   - WHAT: What the skill does (specific capabilities)
   - WHEN: When the agent should use it (trigger scenarios)

### Description Examples

```yaml
# PDF Processing
description: Extract text and tables from PDF files, fill forms, merge documents. Use when working with PDF files or when the user mentions PDFs, forms, or document extraction.

# Excel Analysis
description: Analyze Excel spreadsheets, create pivot tables, generate charts. Use when analyzing Excel files, spreadsheets, tabular data, or .xlsx files.

# Git Commit Helper
description: Generate descriptive commit messages by analyzing git diffs. Use when the user asks for help writing commit messages or reviewing staged changes.

# Code Review
description: Review code for quality, security, and best practices following team standards. Use when reviewing pull requests, code changes, or when the user asks for a code review.
```

---

## Core Authoring Principles

### 1. Concise is Key

The context window is shared with conversation history, other skills, and requests. Every token competes for space.

**Default assumption**: The agent is already very smart. Only add context it doesn't already have.

Challenge each piece of information:
- "Does the agent really need this explanation?"
- "Can I assume the agent knows this?"
- "Does this paragraph justify its token cost?"

**Good (concise)**:
```markdown
## Extract PDF text

Use pdfplumber for text extraction:

\`\`\`python
import pdfplumber

with pdfplumber.open("file.pdf") as pdf:
    text = pdf.pages[0].extract_text()
\`\`\`
```

**Bad (verbose)**:
```markdown
## Extract PDF text

PDF (Portable Document Format) files are a common file format that contains
text, images, and other content. To extract text from a PDF, you'll need to
use a library. There are many libraries available for PDF processing, but we
recommend pdfplumber because it's easy to use and handles most cases well...
```

### 2. Keep SKILL.md Under 500 Lines

For optimal performance, the main SKILL.md file should be concise. Use progressive disclosure for detailed content.

### 3. Progressive Disclosure

Put essential information in SKILL.md; detailed reference material in separate files that the agent reads only when needed.

```markdown
# PDF Processing

## Quick start
[Essential instructions here]

## Additional resources
- For complete API details, see [reference.md](reference.md)
- For usage examples, see [examples.md](examples.md)
```

**Keep references one level deep** - link directly from SKILL.md to reference files. Deeply nested references may result in partial reads.

### 4. Set Appropriate Degrees of Freedom

Match specificity to the task's fragility:

| Freedom Level | When to Use | Example |
|---------------|-------------|---------|
| **High** (text instructions) | Multiple valid approaches, context-dependent | Code review guidelines |
| **Medium** (pseudocode/templates) | Preferred pattern with acceptable variation | Report generation |
| **Low** (specific scripts) | Fragile operations, consistency critical | Database migrations |

---

## Utility Scripts

Pre-made scripts offer advantages over generated code:
- More reliable than generated code
- Save tokens (no code in context)
- Save time (no code generation)
- Ensure consistency across uses

```markdown
## Utility scripts

**analyze_form.py**: Extract all form fields from PDF
\`\`\`bash
python scripts/analyze_form.py input.pdf > fields.json
\`\`\`

**validate.py**: Check for errors
\`\`\`bash
python scripts/validate.py fields.json
# Returns: "OK" or lists conflicts
\`\`\`
```

Make clear whether the agent should **execute** the script (most common) or **read** it as reference.

### Tool References in Skills

When referencing tools in SKILL.md or prompt templates, use capability-based descriptions instead of platform-specific tool names. Skills may run on different agent systems (Claude Code, Cursor, Cline, etc.) where tool names differ.

- ✅ "Run the script using the shell/terminal" (not "use the Bash tool")
- ✅ "Read the file" (not "use the Read tool")
- ✅ "Write the output to the file" (not "use the Write tool")
- ✅ "Search for files matching the pattern" (not "use the Glob tool")

For `<tools>` sections in prompt templates, see `debug-guidelines.md` § Tool Naming for the full mapping table.

---

## Anti-Patterns to Avoid

### 1. Windows-Style Paths
- ✅ Use: `scripts/helper.py`
- ❌ Avoid: `scripts\helper.py`

### 2. Too Many Options
```markdown
# Bad - confusing
"You can use pypdf, or pdfplumber, or PyMuPDF, or..."

# Good - provide a default with escape hatch
"Use pdfplumber for text extraction.
For scanned PDFs requiring OCR, use pdf2image with pytesseract instead."
```

### 3. Time-Sensitive Information
```markdown
# Bad - will become outdated
"If you're doing this before August 2025, use the old API."

# Good - use an "old patterns" section
## Current method
Use the v2 API endpoint.

## Old patterns (deprecated)
<details>
<summary>Legacy v1 API</summary>
...
</details>
```

### 4. Inconsistent Terminology
Choose one term and use it throughout:
- ✅ Always "API endpoint" (not mixing "URL", "route", "path")
- ✅ Always "field" (not mixing "box", "element", "control")

### 5. Vague Skill Names
Use verb-object form to make the action and target clear:
- ✅ Good: `process-pdf`, `analyze-spreadsheet`, `review-pr`
- ❌ Avoid: `helper`, `utils`, `tools`, `pdf-stuff`

---

## Skill Creation Workflow

Phases 1-2 apply to **Create** mode only. **Fix** mode starts at Phase 3 for the specific
change requested; **Review** mode starts at Phase 4, applied read-only against the
existing skill. See § Mode above.

### Phase 1: Discovery (Create mode)

Gather the requirements listed in "Before You Begin: Gather Requirements" above (purpose and scope, skill category, storage location, trigger scenarios, domain knowledge, output format, existing patterns), using a structured question tool if available.

### Phase 2: Design (Create mode)

1. Draft the skill name (lowercase, hyphens, max 64 chars). Folder name must match.
2. Write a specific, third-person description with WHAT + WHEN + trigger phrases
3. Outline the main sections needed
4. Identify if supporting files or scripts are needed

### Phase 3: Implementation

1. Create the directory structure
2. Write the SKILL.md file with frontmatter
3. Create any supporting reference files
4. Create any utility scripts if needed

**Fix mode**: apply only the requested change to the existing files above — don't
redo steps that aren't affected by it, and don't refactor unrelated parts of the skill.

### Phase 4: Verification (also the Review-mode checklist)

In **Review mode**, run this list read-only against the existing skill and report
each failing item as a finding (file + what's wrong); do not edit anything.

1. Verify the SKILL.md is under 500 lines
2. Check that the description is specific and includes trigger terms
3. Ensure consistent terminology throughout
4. Verify all file references are one level deep
5. Security check:
   - No XML angle brackets (`<` `>`) in frontmatter (rejected by some platforms; see "Security restrictions in frontmatter")
   - No `README.md` inside the skill folder (the spec allows extra files, but documentation belongs in SKILL.md or references/ — a README duplicates SKILL.md)
   - Folder name matches `name` field
6. Validate that the frontmatter parses as YAML — run the bundled validator and confirm it prints `OK`:
   ```bash
   uv run --script ${CLAUDE_SKILL_DIR}/scripts/validate_frontmatter.py path/to/skill/SKILL.md
   ```
   (`${CLAUDE_SKILL_DIR}` resolves to this skill's directory on Claude Code; on other platforms, use the path to this skill's `scripts/` folder.)
   It checks that the frontmatter parses as YAML, that `name`/`description` are present, that `description` is ≤1024 chars, that no frontmatter value contains `<`/`>`, and that `name` is kebab-case, ≤64 chars, and matches the folder name. If YAML parsing fails, the usual cause is an unquoted `description` — quote it (see "Security restrictions in frontmatter" above). If `uv`/`pyyaml` is unavailable, manually confirm every string value is double-quoted.
7. Test that the skill can be discovered and applied

For common issues and their solutions, see [security-and-troubleshooting.md](security-and-troubleshooting.md).

---

## Summary Checklist

Before finalizing a skill, verify:

### Core Quality
- [ ] Description is specific and includes key terms
- [ ] Description includes both WHAT and WHEN
- [ ] Written in third person
- [ ] SKILL.md body is under 500 lines
- [ ] Consistent terminology throughout
- [ ] Examples are concrete, not abstract

### Security & Naming
- [ ] No XML angle brackets in frontmatter
- [ ] Folder name matches `name` field
- [ ] No `README.md` inside skill folder (docs go in SKILL.md or references/)

### Structure
- [ ] File references are one level deep
- [ ] Progressive disclosure used appropriately
- [ ] Workflows have clear steps
- [ ] No time-sensitive information

### If Including Scripts
- [ ] Scripts solve problems rather than punt
- [ ] Required packages are documented
- [ ] Error handling is explicit and helpful
- [ ] No Windows-style paths
- [ ] Error categories defined (immediate termination vs fallback)
- [ ] Tool names in `<tools>` use capability-based naming

## Guidelines for patterns and examples

Read `patterns-and-examples.md` during the Design phase to select appropriate
structural patterns for the skill. Indicators:
- The skill needs output templates, workflow steps, or validation loops
- You need a complete example to calibrate quality and structure

## Guidelines for debug handling and tool portability

Read `debug-guidelines.md` when the skill includes scripts or external tool execution
that can fail at runtime. Indicators:
- The skill invokes Python scripts, shell commands, or SQL
- Execution errors need structured fallback or retry logic
- The skill uses SubAgent delegation and may need isolated debug sessions

## Guidelines for subagent delegation skills

Read `create-skill-context.md` when the skill being created involves a workflow that can be
decomposed into multiple subtasks delegated to SubAgents. Indicators:
- The workflow has 2+ distinct task types that can run independently
- Sequential execution in a single thread would exhaust the context window
- The user mentions parallel execution, delegation, or multi-agent orchestration

It uses two companion files in this skill's directory: `subagent-protocol.md` (the generic
delegation protocol — create-skill's authoring reference, applied when filling in each
prompt.md, not copied into the new skill) and `prompt.md` (the prompt template stub,
copied and filled in per task type).

## Guidelines for writing instruction wording

Read `instruction-writing-tips.md` when writing the wording of a skill's
instructions — SKILL.md body, `prompt.md` templates, or `subagent-protocol.md`
gates — and you need an agent to reliably follow a directive rather than
merely be aware of it. Indicators:
- The skill has an ordered set of instructions where later steps depend on
  earlier ones being resolved correctly first
- The skill includes a gate, validation step, or "if unsure, ask" branch
- A `prompt.md` template will be reused across task types with values that
  vary per invocation

## Guidelines for Claude Code-specific features

Read `advanced-mode.md` when the skill targets Claude Code and may benefit from platform
extensions beyond the Agent Skills open standard. Indicators:
- The skill needs slash-command arguments, autocomplete hints, or user-only invocation
- The skill should run in an isolated context (`context: fork`) or restrict available tools
- You want dynamic context injection (shell output embedded at render time), lifecycle
  hooks, or model/effort overrides

Note: these are Claude Code extensions — skills relying on them are not portable to other
agent platforms.
