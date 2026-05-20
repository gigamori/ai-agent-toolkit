<!-- taskflow guidelines keyword reminder -->
<!-- Source: progress_guidelines.md, notes_guidelines.md, tasks_guidelines.md -->
<!-- When updating any of the 3 source guidelines, update this file too (see README.md) -->

[taskflow guidelines reminder]
PROHIBIT: no status: or summary: in task frontmatter, no category: in frontmatter, no hand-edit inside <!-- @table:begin/end -->, no multiple table regions (exactly 1 per project), no auto-move to 2_done/ without human approval, no edit/reorder/delete inside <!-- @log -->, no Session Log/Last Updated/Completed Tasks sections in progress.md
FORMAT: task filename <YYYY-MM-DD>_<topic-slug>.md, slug kebab-case ≤50 chars, priority: HIGH|MID|LOW required, index.md Description ≤100 chars, notes slug ≤30 chars, log append-only between <!-- @log:begin/end -->
AUTHORITY: folder location = single authority for status, H1 = summary/title, priority: in frontmatter, category = folder not frontmatter, progress.md table = cache (never authoritative)
NOTES: 6 fixed categories (specs/ investigations/ checks/ procedures/ backlog/ _archive/), auto-save requires user confirmation, code-derivable info must not be saved, temporary single-session memos must not be saved
AUTOSAVE: project_notes_autosave: true → deliver result then ask save confirmation, false → do not ask, manual "save to notes" → skip confirmation, propose appending to existing note when topic overlaps
TASK WRITE: body region = mutable (replace fully), log region = append-only, update updated: on every modify, 0_todo → 1_in_progress via /progress start, 2_done/ requires /progress approve, new task default 0_todo (if ambiguous ask user), distill durable knowledge from 2_done/ into project-notes/
