import json, sys, os, tempfile

# Cursor sends hook stdin with a UTF-8 BOM; Claude Code does not.
try:
    data = json.loads(sys.stdin.buffer.read().decode('utf-8-sig'))
except Exception:
    sys.exit(0)

session_id = data.get('session_id', '')
tool_name  = data.get('tool_name', '')
tool_input = data.get('tool_input', {})

# Cursor and Claude Code both use "Read" for the read tool; no alias needed here.
if not session_id or tool_name != 'Read':
    sys.exit(0)

file_path = tool_input.get('file_path', '')
if not file_path or not os.path.isfile(file_path):
    sys.exit(0)

file_path_norm = os.path.normpath(file_path)

# Append to the session reads log
reads_path = os.path.join(tempfile.gettempdir(), f'claude_reads_{session_id}.txt')
try:
    with open(reads_path, 'a', encoding='utf-8') as f:
        f.write(file_path_norm + '\n')
except Exception:
    pass

# Remove entries from pending that are now read (suffix match against the relative path)
pending_path = os.path.join(tempfile.gettempdir(), f'claude_rules_pending_{session_id}.txt')
if not os.path.exists(pending_path):
    sys.exit(0)

try:
    with open(pending_path, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f.read().splitlines() if l.strip()]

    remaining = [
        line for line in lines
        if not file_path_norm.lower().endswith(os.path.normpath(line).lower())
    ]

    with open(pending_path, 'w', encoding='utf-8') as f:
        if remaining:
            f.write('\n'.join(remaining) + '\n')
except Exception:
    pass
