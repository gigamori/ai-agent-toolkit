<!-- taskflow guidelines reminder — MANIFEST variant -->
<!-- Selected when env TASKFLOW_GUIDELINES_REMINDER=manifest (default: full = guidelines_reminder.md). -->
<!-- (b) block below (ROUTER / RESPONSE LEADING LINES) MUST stay byte-identical to guidelines_reminder.md — enforced by tests/test_guidelines_reminder_mode.sh. -->
<!-- Source: progress_guidelines.md, notes_guidelines.md, tasks_guidelines.md, project_routing.md -->

[taskflow guidelines reminder]
GUIDELINES manifest — full guidelines were injected at session start and re-injected after each compaction; the labels below index them. Before an action one governs, recall the matching rule from that block:
- PROHIBIT: frontmatter / @table / @log / 2_done / progress.md-section bans
- FORMAT: filenames, priority, @log entry form, ≤-limits
- AUTHORITY: folder=status, H1=summary, table=cache
- NOTES: 6 categories, autosave-confirm, no code-derivable
- AUTOSAVE: autosave-signal handling
- TASK WRITE: body-replace / log-append / Next-Steps / status-moves
ROUTER: [Progress Session] with non-empty current_project → invoke subagent taskflow:project-router (Agent tool) BEFORE answering; empty → do not invoke.
RESPONSE LEADING LINES: when a project is assigned, ALWAYS include [pj:<project>] in the response's leading lines (near the beginning, before the main body; it may follow other leading lines such as [Mode:], not necessarily the first line). When unassigned, omit [pj:...] entirely. This applies also to skill/slash-command turns, including literal reply templates. If you did task work WITHOUT editing the task's own tasks/<status>/*.md file (read a task/handoff, produced the result elsewhere), ALSO add [tasks: <file>.md ...] in the leading lines listing the owning task filename(s) worked on this turn (omit when you edited the task file directly, or did no task work).
