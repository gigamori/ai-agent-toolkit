#!/usr/bin/env python3
"""
PostToolUse hook: Auto-rebuild progress.md table after task file writes.

Triggers when Write or Edit targets a file under _projects/<project>/tasks/.
Runs rebuild_progress.py to regenerate the table region in progress.md.
"""
import json, sys, os, re, subprocess

try:
    data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
except Exception:
    sys.exit(0)

file_path = data.get('tool_input', {}).get('file_path', '')
if not file_path:
    sys.exit(0)

normalized = file_path.replace('\\', '/')

# Match _projects/<project>/tasks/<status>/<file>
m = re.search(r'_projects/([^/]+)/tasks/(0_todo|1_in_progress|2_done)/', normalized)
if not m:
    sys.exit(0)

# Derive project dir from the matched path
prefix_end = m.start() + len('_projects/') + len(m.group(1))
project_dir = normalized[:prefix_end]
# Normalise to OS path
project_dir = os.path.normpath(project_dir)

if not os.path.isdir(project_dir):
    sys.exit(0)

# Locate rebuild_progress.py relative to this hook
script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'rebuild_progress.py')
script = os.path.normpath(script)

if not os.path.isfile(script):
    sys.exit(0)

try:
    result = subprocess.run(
        ['uv', 'run', '--no-project', script, project_dir],
        capture_output=True, text=True, encoding='utf-8', timeout=15,
    )
except (subprocess.TimeoutExpired, OSError):
    sys.exit(0)

if result.returncode == 0 and result.stdout.strip():
    session_id = data.get('session_id', '')
    session_tag = f" session={session_id[:8]}" if session_id else ""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[auto-rebuild]{session_tag} {result.stdout.strip()}"
        }
    }
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')

sys.exit(0)
