## Loading

Notes reach you as pointers (`index.md` rows), never as a listing to scan.

- Start from this turn's injected `project-notes (index rows):` block (verbatim `index.md` rows chosen by location — a note anchored on a file you named, read or wrote — and by topic, ending in `project-notes matched: M of N rows (K shown)`) and the router result: `project_notes_relevant` (verbatim rows) and `project_notes_summary` (counts). `_archive/` is excluded from both. Do NOT read `index.md` in full.
- Before opening a candidate note's body, read its frontmatter `summary:` and decide from that.
- When the router rows are insufficient, pull narrowly:
  1. Before editing or investigating a symbol: list the enclosing symbol and its depth-1 callers / callees with the `LSP` tool, then Grep `project-notes/` for those names and for their `path::symbol` form (the `anchors:` notation).
  2. For a topic search, when `project-notes/.qmd/` exists: from `project-notes/`, run `qmd query "$(printf 'vec: %s\nlex: %s\nlex: %s' '<subject>' '<term>' '<term>')" --no-rerank -n 10`. Write the `vec:` subject and one `lex:` line per term yourself; no rerank. A `vec:` line must not contain a `-` right after whitespace (qmd rejects it as negation).
  3. For a note's structure: Grep `^## ` and Read with offset / limit instead of the whole body.
  4. When the `project-notes matched` footer shows more matches than rows shown, narrow by symbol (step 1) instead of asking for more rows.
- If `index.md` is missing, fall back to walking `project-notes/<category>/*.md` and judging from filenames.

## Categories

Notes live in one of 6 fixed categories. The folder is the source of truth for category — do NOT add a `category:` field to frontmatter.

Layout is flat: `project-notes/<category>/<name>.md`. Only `_archive/` may hold sub-directories; `/progress check` reports any other nested directory.

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
summary: >                       # recommended, ≤500 chars
  One paragraph a model can read to judge relevance without opening the body.
anchors: [src/x.py, src/x.py::main]   # optional: repo-relative path or path::symbol
---

# Title

Body content.
```

All frontmatter fields are optional. `created` / `updated` enable stale detection by `/progress check`.

`summary` (≤500 chars) is judgment material: read on demand to decide whether the note is worth opening, without opening it. It is not the same as index.md's `Description` (≤100 chars) — Description exists only so the note can be found in the index listing. Its text is arbitrary: a consumer must never feed it to a regex or a `replace`-style replacement string unescaped (`$&`, `` $` ``, `$1` expand there).

`anchors` lists the code a note is about, as repo-relative `/`-separated paths or `path::symbol` (`Class.method` for members). List only the code the note is about, not every path it mentions. `/progress check` reports an entry whose path does not exist or whose symbol is missing from the query-lsp snapshot.

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
- `File` includes the category prefix (e.g., `specs/api-design.md`). A non-`.md` attachment that lives beside a note (a setup script, a fixture) may have a row too; it is not a note (never pushed, never counted), and `/progress check` accepts the row as long as the file exists.
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
- Do NOT save **LLM-to-LLM** handoffs here — a message written for another LLM session to consume (a cross-session hand-off, a sibling-repo request). They are transient memos, not durable notes. Write them to `_projects/<project>/llm-handoff/` instead — a flat directory, no index (neither `project-notes/index.md` nor one of its own), no status tracking, no progress.md linkage, freely deleted by hand, and never loaded into context unless referenced explicitly (`@llm-handoff/<filename>`). Nothing is distilled out of them before deletion.
  - A handoff a **human** executes — an e2e / debug handoff whose tester is a person and whose filled-in result is a record worth keeping — is durable, and stays in `checks/`. The dividing line is who consumes it, not the word "handoff".
- Do NOT add a `category:` field to frontmatter. The folder is the sole source of truth.
- Do NOT exceed 100 characters in `Description` in `index.md`. Move detail to the note body.
- Do NOT exceed 500 characters in frontmatter `summary:`.
- Do NOT create sub-directories under a category other than `_archive/`.
