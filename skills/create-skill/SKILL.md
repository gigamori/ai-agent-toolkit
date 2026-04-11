---
name: create-skill
description: Guides users through creating effective Agent Skills. Use when you want to create, write, or author a new skill, or asks about skill structure, best practices, or SKILL.md format.
---
# Creating Agent Skills

This skill guides you through creating effective Agent Skills. Skills are markdown files that teach the agent how to perform specific tasks: reviewing PRs using team standards, generating commit messages in a preferred format, querying database schemas, or any specialized workflow. Skills work across Claude.ai, Claude Code, and API.

## Before You Begin: Gather Requirements

Before creating a skill, gather essential information from the user about:

1. **Purpose and scope**: What specific task or workflow should this skill help with? Identify 2-3 concrete use cases.
2. **Skill category**: Which type best fits?
   - Document & Asset Creation (consistent output: docs, presentations, code)
   - Workflow Automation (multi-step processes, coordination)
   - MCP Enhancement (workflow guidance on top of MCP tool access)
3. **Target location**: Personal skill (~/.claude/skills/ or ~/.cursor/skills/) or project skill (.claude/skills/ or .cursor/skills/)?
4. **Trigger scenarios**: When should the agent automatically apply this skill?
5. **Key domain knowledge**: What specialized information does the agent need that it wouldn't already know?
6. **Output format preferences**: Are there specific templates, formats, or styles required?
7. **Existing patterns**: Are there existing examples or conventions to follow?

### Inferring from Context

If you have previous conversation context, infer the skill from what was discussed. You can create skills based on workflows, patterns, or domain knowledge that emerged in the conversation.

### Gathering Additional Information

If you need clarification, use the AskQuestion tool when available:

```
Example AskQuestion usage:
- "Where should this skill be stored?" with options like ["Personal (~/.claude/skills/)", "Project (.claude/skills/)"]
- "Should this skill include executable scripts?" with options like ["Yes", "No"]
```

If the AskQuestion tool is not available, ask these questions conversationally.

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

| Type | Claude Code | Cursor | Scope |
|------|------------|--------|-------|
| Personal | ~/.claude/skills/skill-name/ | ~/.cursor/skills/skill-name/ | Available across all your projects |
| Project | .claude/skills/skill-name/ | .cursor/skills/skill-name/ | Shared with anyone using the repository |

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
| `name` | Yes | Max 64 chars, lowercase letters/numbers/hyphens only, verb-object form. Must match folder name. | Unique identifier |
| `description` | Yes | Max 1024 chars, non-empty, no XML angle brackets (`<` `>`) | Helps agent decide when to apply the skill |
| `license` | No | e.g., MIT, Apache-2.0 | Open-source distribution |
| `allowed-tools` | No | e.g., `"Bash(python:*) WebFetch"` | Restrict tool access |
| `compatibility` | No | 1-500 chars | Environment requirements |
| `metadata` | No | Custom key-value pairs (author, version, mcp-server) | Additional info |

Security restrictions in frontmatter:
- XML angle brackets (`<` `>`) are forbidden (frontmatter appears in system prompt; injection risk)
- Names containing "claude" or "anthropic" are reserved

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

When helping a user create a skill, follow this process:

### Phase 1: Discovery

Gather information about:
1. The skill's purpose and primary use case
2. Storage location (personal vs project)
3. Trigger scenarios
4. Any specific requirements or constraints
5. Existing examples or patterns to follow

If you have access to the AskQuestion tool, use it for efficient structured gathering. Otherwise, ask conversationally.

### Phase 2: Design

1. Draft the skill name (lowercase, hyphens, max 64 chars). Folder name must match.
2. Write a specific, third-person description with WHAT + WHEN + trigger phrases
3. Outline the main sections needed
4. Identify if supporting files or scripts are needed

### Phase 3: Implementation

1. Create the directory structure
2. Write the SKILL.md file with frontmatter
3. Create any supporting reference files
4. Create any utility scripts if needed

### Phase 4: Verification

1. Verify the SKILL.md is under 500 lines
2. Check that the description is specific and includes trigger terms
3. Ensure consistent terminology throughout
4. Verify all file references are one level deep
5. Security check:
   - No XML angle brackets (`<` `>`) in frontmatter
   - Name does not contain "claude" or "anthropic"
   - No `README.md` inside the skill folder
   - Folder name matches `name` field
6. Test that the skill can be discovered and applied

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
- [ ] Name does not contain "claude" or "anthropic"
- [ ] Folder name matches `name` field
- [ ] No `README.md` inside skill folder

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
