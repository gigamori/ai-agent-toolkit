#!/usr/bin/env python3
"""
Stop hook: Inject a [progress capture] system-reminder when the session
touched any task-relevant file via Write / Edit / NotebookEdit / file-moving
Bash. Prompts the LLM to update `## Next Steps` of the relevant task before
the session truly ends.

For the design, see project-notes/specs/progress-audit-design.md §3
(Capture mechanism).

Behavior:
  - Reads project state from _projects/_state/<session_id>.json (set by
    session_init.py).
  - Scans the session jsonl at ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
    for assistant tool_use blocks; collects file paths from Write / Edit /
    NotebookEdit and 2nd/3rd args from `mv` / `cp` / `rm` Bash invocations.
  - If touched list is empty (read-only session), exits 0 without injection.
  - Otherwise returns {"decision": "block", "reason": "..."} so the LLM
    continues for one more turn to perform the capture, then stops.
  - Marks `progress_capture_done: true` in the state file to prevent
    re-injection on the LLM's follow-up Stop.

Bypass: same `norouter` token semantics as session_init.py (handled by the
state file: if session_init exited without writing state, no state_path
exists and this hook also exits 0).
"""
import datetime
import json
import os
import shlex
import sys

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')
_cwd = os.getcwd().replace('\\', '/')
_encoded = _cwd.lower().replace(':', '-').replace('/', '-')
JSONL_DIR = os.path.expanduser(f'~/.claude/projects/{_encoded}')

WRITE_TOOLS = {'Write', 'Edit'}
NOTEBOOK_TOOL = 'NotebookEdit'
BASH_FILE_VERBS = {'mv', 'cp', 'rm'}
MAX_TOUCHED_IN_INJECTION = 30


def normalize_path(p: str, cwd: str) -> str:
    if not p:
        return p
    p = p.replace('\\', '/')
    cwd_norm = cwd.replace('\\', '/').rstrip('/')
    # Case-insensitive prefix match (Windows paths are case-insensitive)
    if p.lower().startswith(cwd_norm.lower() + '/'):
        return p[len(cwd_norm) + 1:]
    return p


def extract_bash_paths(cmd: str) -> list[str]:
    """For `mv|cp|rm <paths...>`, return the non-flag arguments."""
    if not cmd or not isinstance(cmd, str):
        return []
    try:
        tokens = shlex.split(cmd, posix=True)
    except ValueError:
        return []
    if not tokens or tokens[0] not in BASH_FILE_VERBS:
        return []
    return [t for t in tokens[1:] if not t.startswith('-')]


def extract_touched(jsonl_path: str, cwd: str) -> list[str]:
    if not os.path.isfile(jsonl_path):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def push(p):
        if not p:
            return
        n = normalize_path(p, cwd)
        if n not in seen:
            seen.add(n)
            out.append(n)

    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get('type') != 'assistant':
                    continue
                msg = rec.get('message') or {}
                content = msg.get('content') or []
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') != 'tool_use':
                        continue
                    name = block.get('name') or ''
                    inp = block.get('input') or {}
                    if not isinstance(inp, dict):
                        continue
                    if name in WRITE_TOOLS:
                        push(inp.get('file_path', ''))
                    elif name == NOTEBOOK_TOOL:
                        push(inp.get('notebook_path', ''))
                    elif name == 'Bash':
                        for p in extract_bash_paths(inp.get('command', '')):
                            push(p)
    except OSError:
        pass
    return out


def main() -> int:
    if not os.path.isdir(PROGRESS_ROOT):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0
    session_id = data.get('session_id', '')
    if not session_id:
        return 0
    state_path = os.path.join(STATE_DIR, f'{session_id}.json')
    if not os.path.exists(state_path):
        return 0
    try:
        with open(state_path, 'r', encoding='utf-8') as f:
            state = json.load(f)
    except Exception:
        return 0

    if state.get('progress_capture_done'):
        return 0

    project = state.get('project', '')
    if not project:
        return 0
    if not os.path.isdir(os.path.join(PROGRESS_ROOT, project)):
        return 0

    jsonl_path = os.path.join(JSONL_DIR, f'{session_id}.jsonl')
    touched = extract_touched(jsonl_path, os.getcwd())

    # Mark done regardless of injection (prevents re-fire on LLM's follow-up Stop)
    state['progress_capture_done'] = True
    try:
        with open(state_path, 'w', encoding='utf-8') as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass

    if not touched:
        return 0

    sid8 = session_id[:8]
    date = datetime.date.today().isoformat()
    shown = touched[:MAX_TOUCHED_IN_INJECTION]
    tail = '' if len(touched) <= MAX_TOUCHED_IN_INJECTION else f' ...({len(touched) - MAX_TOUCHED_IN_INJECTION} more)'

    reason = (
        f'[progress capture] session={sid8} date={date}\n'
        f'touched: {" ".join(shown)}{tail}\n\n'
        f'For each task you advanced:\n'
        f'- unresolved next step → update `## Next Steps`; create task if missing '
        f'(not-yet-started → 0_todo/, in-flight → 1_in_progress/)\n'
        f'- complete → clear `## Next Steps` and append to `<!-- @log -->`: '
        f'`- {date} [s:{sid8}]: completed`\n'
        f'No real work → skip.'
    )

    result = {'decision': 'block', 'reason': reason}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
