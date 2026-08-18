---
name: session-handoff
description: "Write a handoff document for another LLM session. Trigger: /session-handoff, 別セッションに依頼/引き継ぎ, context handoff."
---

# Session handoff

Write a handoff so another session can perform the work (`$ARGUMENTS`; if
empty, the current task context). What to include is per-case judgment —
resolve details here or delegate them to the consumer, whichever fits.

Destination — decided solely by `current_project` in this turn's
`[Progress Session]` context line. Do not scan the workspace. Do not interpret
`pj:` tokens yourself.

1. `current_project=<non-empty>` readable →
   `_projects/<current_project>/llm-handoff/<YYYY-MM-DD>_<topic-slug>.md`
2. Not readable (no such line, or empty) →
   `<cwd>/_handoffs/llm/<YYYY-MM-DD>_<topic-slug>.md` — `<cwd>` is the
   session's working directory; do not walk up to a git root or any parent.

The user retargets with `pj:<name>` and forces case 2 with `pj:none`; taskflow
resolves both before this skill runs, so just read the resulting header.

Create the directory as needed. Never under `project-notes/`; no index or
progress.md entry. The file is transient — deletable after consumption.

Format: no frontmatter. Line 1 = `# <title>` — the title alone, with no
`Handoff:` or other prefix. Line 2 =
`handed_from: s:${CLAUDE_SESSION_ID} (pj:<project>) / created: <YYYY-MM-DD>`
— the session id is always present; `(pj:<project>)` only in case 1.
