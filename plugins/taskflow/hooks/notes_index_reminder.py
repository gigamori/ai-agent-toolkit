#!/usr/bin/env python3
"""
PreToolUse hook: Inject project-notes operational rules when accessing
_projects/<project>/project-notes/ files (excluding index.md itself).

Ensures the LLM remembers to keep project-notes/index.md in sync.
Fires on Write|Edit only (hooks.json), so a delete or a category `mv`
done via Bash does NOT trigger it — those are caught after the fact by
check_progress.py::check_notes_index_consistency (`/progress check`).
"""
import json, sys, re

try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
except Exception:
  sys.exit(0)

file_path = data.get('tool_input', {}).get('file_path', '')
if not file_path:
  sys.exit(0)

normalized = file_path.replace('\\', '/')

# Match _projects/<project>/project-notes/<file> but NOT index.md
m = re.search(r'_projects/([^/]+)/project-notes/(?!index\.md$)(.+)', normalized)
if not m:
  sys.exit(0)

project = m.group(1)

# The 4-column row here MUST match the `index.md format` section of
# prompts/notes_guidelines.md. tests/test_notes_index_reminder.py pins the two
# together so this literal cannot silently drift from the canon again.
rules = (
  f"[Project Notes Index Rule — {project}]\n"
  "This file is a document under project-notes/. After the operation completes, synchronize project-notes/index.md:\n"
  "- New file → add a 4-column row: | File | Description | Tags | Updated |\n"
  "  File = path with its category prefix (e.g. specs/api-design.md); Updated = YYYY-MM-DD\n"
  "- Update (when Description/Tags change) → update the matching row and its Updated\n"
  "- Delete → remove the matching row"
)

result = {
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "additionalContext": rules
  }
}

sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
sys.stdout.buffer.write(b'\n')
sys.exit(0)
