## Loading

- At the start of work, read `project-notes/index.md` and select files relevant to the task.
- If `index.md` is missing, fall back to walking `project-notes/<category>/*.md` and judging from filenames.

## Categories

Notes live in one of 6 fixed categories. The folder is the source of truth for category — do NOT add a `category:` field to frontmatter.

| Category | Purpose | Examples |
|---|---|---|
| `specs/` | Designs, decisions, ADRs, proposals | API spec, ADR, business proposal |
| `investigations/` | Findings and results of research, analysis, post-mortems, retrospectives | Code investigation report, market analysis results, incident retro |
| `checks/` | Verification items, checklists (no judgment, just confirm) | Test specs (TC-1..), audit checklist |
| `procedures/` | Step-by-step instructions for humans or LLMs | Runbook, SOP, troubleshooting guide, agent instruction script |
| `backlog/` | Candidate items, ideas, issue tracker entries | Feature backlog, issue list, initiative ideas |
| `_archive/` | Exhausted; no longer authoritative | Old specs, deprecated docs |

## File format

```markdown
---
domain: development              # optional: development | business | strategy | ops | research | ...
kind: postmortem                 # optional: free-form subtype
created: 2026-05-13              # recommended
updated: 2026-05-13              # recommended
tags: [extension, hardening]     # optional
---

# Title

Body content.
```

All frontmatter fields are optional. `created` / `updated` enable stale detection by `/progress check`.

## index.md format

```markdown
| File | Description | Tags | Updated |
|------|-------------|------|---------|
| specs/api-design.md | API design for the X module | api, design | 2026-05-13 |
```

Operational rules:

- When a note is created → add a row.
- When a note's Description / Tags change → update the row.
- When a note is deleted → remove the row.
- `Description` MUST be ≤ 100 characters. Put longer summaries in the note body, not the index.
- `File` includes the category prefix (e.g., `specs/api-design.md`).
- `Updated` is `YYYY-MM-DD`, taken from the note's frontmatter `updated:` or file mtime.

## What to save

- Initial repository investigation (structure, tech stack, design philosophy) → `specs/` or `investigations/`
- Codebase understanding memos → `investigations/`
- Explore-agent findings reusable across tasks → `investigations/`
- Test specifications, verification criteria → `checks/`
- Operational runbooks, SOPs → `procedures/`
- Feature / issue backlogs, candidate ideas → `backlog/`

## Auto-save flow

When the router returns `project_notes_autosave: true`, the main agent should:

1. Answer the user's request normally (deliver the investigation result as the main response).
2. At the end of the response, ask for save confirmation in this form:

   > Save this investigation result as `_projects/<project>/project-notes/<category>/<slug>.md`?
   > Suggested category: `<category>`
   > Suggested slug: `<kebab-case-slug>`

3. Only if the user approves:
   - Create the file with frontmatter + H1 + body. Always include source paths / line numbers when summarizing code.
   - Append a row to `project-notes/index.md`.

4. If the user declines or does not respond, do NOT save.

When `project_notes_autosave: false`, do NOT ask (respond normally).

## Slug rules

- kebab-case, under 30 characters, noun phrase capturing the content.
- When digging deeper into a topic already covered by an existing note, propose appending to that file rather than creating a new one (mention this in the confirmation step).

## Manual save (explicit user instruction)

When the user explicitly says "save this to notes" (or equivalent), skip the confirmation step and save immediately. Ask only if category or slug is genuinely ambiguous.

## Prohibitions

- Do NOT save information that can be derived from code (function signatures, file paths). It decays as code evolves.
- Do NOT save temporary memos that are only useful within a single session.
- Do NOT add a `category:` field to frontmatter. The folder is the sole source of truth.
- Do NOT exceed 100 characters in `Description` in `index.md`. Move detail to the note body.
