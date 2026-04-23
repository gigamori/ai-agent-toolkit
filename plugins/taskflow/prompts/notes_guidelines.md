## Loading

- At the start of work, read `project-notes/index.md` and select the files relevant to the task.
- If `index.md` does not exist, fall back to listing the files in `project-notes/` and judging from filenames.

## index.md

Each project maintains a file list of its notes in `project-notes/index.md`.

Format:
```markdown
| File | Description | Tags |
|------|-------------|------|
| codebase-survey.md | Repository structure investigation | architecture, overview |
```

Operational rules:
- When a note is created, add an entry to `index.md`.
- When a note is updated and its Description/Tags change, update the entry.
- When a note is deleted, remove its entry.

## What to save

- Initial repository investigation (structure, tech stack, design philosophy)
- Codebase understanding memos (core modules, data flow, dependencies)
- Explore-agent findings that are reusable

## Auto-save flow

When the router returns `project_notes_autosave: true`, the main agent should follow this flow:

1. Answer the user's request normally (deliver the investigation result as the main response).
2. At the end of the response, ask the user for save confirmation in the following form:

   > Save this investigation result as `_projects/<project>/project-notes/<slug>.md`?
   > Suggested slug: `<kebab-case-slug>`

3. Only if the user approves:
   - Create `_projects/<project>/project-notes/<slug>.md` (organize the key points from the main response and always include source paths and line numbers).
   - Append a row to `_projects/<project>/project-notes/index.md` (in the table format above).
4. If the user declines or does not respond, do NOT save.

When `project_notes_autosave: false`, do NOT ask this question (just respond normally).

## slug rules

- kebab-case, under 30 characters, a noun phrase that captures the content.
- When digging deeper into a topic already covered by an existing note, you MAY propose appending to the existing file rather than creating a new one (mention this explicitly in the confirmation step).

## Manual save (explicit user instruction)

When the user explicitly says "save this to notes" or similar, skip the confirmation step of the auto-save flow and save immediately.

## Prohibitions

- Do NOT save information that can be derived directly from code (function signatures, etc.). It decays as the code evolves.
- Do NOT save temporary memos that are only useful within a single session.
