#!/usr/bin/env python3
"""
PreToolUse hook: Inject project-notes operational rules when accessing
_projects/<project>/project-notes/ files (excluding index.md itself).

Ensures the LLM remembers to keep project-notes/index.md in sync
when creating, updating, or deleting project-notes files.
"""
import json, sys, re

try:
  data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
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

rules = (
  f"[Project Notes Index Rule — {project}]\n"
  "This file is a document under project-notes/. After the operation completes, synchronize project-notes/index.md:\n"
  "- New file → add a row: | File | Description | Tags |\n"
  "- Update (when Description/Tags change) → update the matching row\n"
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
