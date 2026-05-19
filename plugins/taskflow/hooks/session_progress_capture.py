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
import re
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
# Split bash commands at chain operators (&&, ||, ;, |) so that args from one
# segment do not bleed into the verb-extraction of another. Greedy `\|\|` first
# to avoid matching as two singles.
_BASH_CHAIN_SPLIT = re.compile(r'\s*(?:&&|\|\||[;|])\s*')
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
    """For each `mv|cp|rm <paths...>` segment in a bash command, return the
    non-flag arguments.

    The command is split at shell chain operators (`&&`, `||`, `;`, `|`)
    first, so a `cp ...` followed by `&& echo ...` does not pull `echo`'s
    args into `cp`'s path list. Each segment is parsed independently with
    shlex, and only segments whose first token is `mv` / `cp` / `rm`
    contribute paths.
    """
    if not cmd or not isinstance(cmd, str):
        return []
    paths: list[str] = []
    for segment in _BASH_CHAIN_SPLIT.split(cmd):
        if not segment.strip():
            continue
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue
        if not tokens or tokens[0] not in BASH_FILE_VERBS:
            continue
        paths.extend(t for t in tokens[1:] if not t.startswith('-'))
    return paths


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

    # The "already fired this session" flag lives in a sidecar marker file
    # (<sid>.captured) rather than in the JSON state. Other components
    # (session_init.py, the project-router subagent) freely rewrite the
    # state JSON every turn and would clobber an inline boolean field —
    # observed empirically as the Stop hook re-firing on every turn.
    capture_marker = os.path.join(STATE_DIR, f'{session_id}.captured')
    if os.path.exists(capture_marker):
        return 0

    project = state.get('project', '')
    if not project:
        return 0
    if not os.path.isdir(os.path.join(PROGRESS_ROOT, project)):
        return 0

    jsonl_path = os.path.join(JSONL_DIR, f'{session_id}.jsonl')
    touched = extract_touched(jsonl_path, os.getcwd())

    if not touched:
        return 0

    # Mark done to prevent re-fire on LLM's follow-up Stop
    try:
        with open(capture_marker, 'w', encoding='utf-8') as f:
            f.write('')
    except OSError:
        pass

    sid8 = session_id[:8]
    date = datetime.date.today().isoformat()
    shown = touched[:MAX_TOUCHED_IN_INJECTION]
    tail = '' if len(touched) <= MAX_TOUCHED_IN_INJECTION else f' ...({len(touched) - MAX_TOUCHED_IN_INJECTION} more)'

    reason = (
        f'[progress capture] session={sid8} date={date}\n'
        f'touched: {" ".join(shown)}{tail}\n\n'
        f'Procedure (do not shortcut):\n'
        f'1. For every `tasks/<status>/*.md` in `touched`: locate the task in its '
        f'CURRENT folder (it may have moved this session). If `<!-- @log -->` does '
        f'not already reflect this session\'s work on that task, append '
        f'`- {date} [s:{sid8}]: <one-line summary>`. Adjust `## Next Steps`: write '
        f'remaining items, or clear (header only) if the task is complete. Create '
        f'a new task in `0_todo/` or `1_in_progress/` if no task file exists for '
        f'work you did.\n'
        f'2. For `touched` paths outside `tasks/` (source files, specs, configs): '
        f'map each to the owning task (by scope, any status) and update per (1). '
        f'Bug fixes and verification-driven tweaks ARE task progress.\n'
        f'3. Reply `[progress capture] skip — no task work` ONLY IF every `touched` '
        f'entry is unrelated to any task in this project (e.g., transient scratch '
        f'files, generated artifacts). Skipping while a touched entry maps to a '
        f'task — even loosely — is wrong; update that task\'s log instead.'
    )

    result = {'decision': 'block', 'reason': reason}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
