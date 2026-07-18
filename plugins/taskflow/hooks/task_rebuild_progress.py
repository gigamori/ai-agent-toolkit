#!/usr/bin/env python3
"""
PostToolUse hook: Auto-rebuild progress.md table after task file changes.

Triggers when Write or Edit targets a file under _projects/<project>/tasks/,
or when a Bash command references such a path (e.g. the `mv` used by
/progress start / approve / unstart — a rename alone never fires Write|Edit,
which left progress.md stale until the next in-place file edit).
Runs rebuild_progress.py to regenerate the table region in progress.md.
"""
import json, sys, os, re, subprocess

try:
    data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
except Exception:
    sys.exit(0)

tool_input = data.get('tool_input', {})

TASKS_SEG = r'/tasks/(?:0_todo|1_in_progress|2_done)/'


def dirs_from_file_path(file_path):
    """Write|Edit: file_path IS the path — slice the project dir prefix."""
    normalized = file_path.replace('\\', '/')
    m = re.search(r'_projects/([^/]+)' + TASKS_SEG, normalized)
    if not m:
        return []
    prefix_end = m.start() + len('_projects/') + len(m.group(1))
    return [os.path.normpath(normalized[:prefix_end])]


def dirs_from_command(command):
    """Bash: scan the command string for task-path references (best effort;
    quoted paths containing spaces are skipped — the /progress skill also
    runs an explicit rebuild after every transition)."""
    normalized = command.replace('\\', '/')
    dirs = []
    for m in re.finditer(r'[^\s"\']*_projects/[^/\s"\']+(?=' + TASKS_SEG + r')',
                         normalized):
        d = os.path.normpath(m.group(0))
        if d not in dirs:
            dirs.append(d)
    return dirs


file_path = tool_input.get('file_path', '')
if file_path:
    project_dirs = dirs_from_file_path(file_path)
    from_bash = False
else:
    command = tool_input.get('command', '')
    project_dirs = dirs_from_command(command) if command else []
    from_bash = True

project_dirs = [d for d in project_dirs if os.path.isdir(d)]
if not project_dirs:
    sys.exit(0)

# Locate rebuild_progress.py relative to this hook
script = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'rebuild_progress.py')
script = os.path.normpath(script)

if not os.path.isfile(script):
    sys.exit(0)

outputs = []
for project_dir in project_dirs:
    try:
        result = subprocess.run(
            ['uv', 'run', '--no-project', script, project_dir],
            capture_output=True, text=True, encoding='utf-8', timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        # Rebuild failures must be visible — a silently stale progress.md is
        # exactly the defect class this hook exists to prevent.
        outputs.append(f"FAILED {project_dir}: {type(e).__name__}")
        continue
    if result.returncode != 0:
        err_lines = (result.stderr or '').strip().splitlines()
        err_head = f" {err_lines[-1]}" if err_lines else ""
        outputs.append(f"FAILED {project_dir}: rc={result.returncode}{err_head}")
        continue
    out = result.stdout.strip()
    if not out:
        continue
    # Bash triggers fire on ANY command that mentions a task path (cat/ls/
    # grep included) — suppress the no-op "unchanged" ack there to avoid
    # recurring context noise. Write|Edit keeps it as a hook-ran ack.
    if from_bash and out.startswith('unchanged'):
        continue
    outputs.append(out)

if outputs:
    session_id = data.get('session_id', '')
    session_tag = f" session={session_id[:8]}" if session_id else ""
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"[auto-rebuild]{session_tag} " + " | ".join(outputs)
        }
    }
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')

sys.exit(0)
