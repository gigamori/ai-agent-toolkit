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
  - Marks completion via a sidecar marker file (`{session_id}.captured`)
    rather than in the state JSON to avoid conflicts with concurrent state
    rewrites by other hooks (session_init.py, project-router).

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
# Split bash commands at chain operators (&&, ||, ;) so that args from one
# segment do not bleed into the verb-extraction of another.  Pipe `|` is NOT
# a split point here; instead, token-level extraction stops at `|` to avoid
# pulling piped-command args into the file-verb's path list.
_BASH_CHAIN_SPLIT = re.compile(r'\s*(?:&&|\|\||;)\s*')
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

    The command is split at shell chain operators (`&&`, `||`, `;`) first,
    so a `cp ...` followed by `&& echo ...` does not pull `echo`'s args
    into `cp`'s path list.  Pipe `|` is handled at the token level: path
    collection stops when a bare `|` token is encountered.  Each segment
    is parsed independently with shlex, and only segments whose first
    token is `mv` / `cp` / `rm` contribute paths.
    """
    if not cmd or not isinstance(cmd, str):
        return []
    paths: list[str] = []
    for segment in _BASH_CHAIN_SPLIT.split(cmd):
        if not segment.strip():
            continue
        try:
            tokens = shlex.split(segment, posix=(os.name != 'nt'))
        except ValueError:
            print(f'[progress_capture] shlex parse error: {segment[:80]}', file=sys.stderr)
            continue
        if not tokens or tokens[0] not in BASH_FILE_VERBS:
            continue
        for t in tokens[1:]:
            if t == '|':
                break
            if not t.startswith('-'):
                paths.append(t)
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
                    print(f'[progress_capture] skipping malformed JSONL line: {line[:120]}', file=sys.stderr)
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


_MARKER_MAX_AGE_DAYS = 7


def _cleanup_stale_markers(state_dir: str) -> None:
    """Remove .captured marker files older than _MARKER_MAX_AGE_DAYS."""
    try:
        cutoff = datetime.datetime.now().timestamp() - _MARKER_MAX_AGE_DAYS * 86400
        for name in os.listdir(state_dir):
            if not name.endswith('.captured'):
                continue
            path = os.path.join(state_dir, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


def main() -> int:
    if not os.path.isdir(PROGRESS_ROOT):
        return 0
    _cleanup_stale_markers(STATE_DIR)
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

    # --- State recovery: pj prefix from assistant response ---
    # If state has no project but the assistant's response contains a
    # [pj:<name>] line in the leading lines, recover the project into state.
    # The [pj:...] line may not be on line 1 — it shares the leading-line
    # region with other plugins' leading lines (e.g. [Mode:]) and the order
    # is unspecified. Self-heals cases where session_init failed to write the
    # project on the first turn or fork inherited an empty parent state.
    if not state.get('project'):
        assistant_msg = data.get('last_assistant_message', '')
        if isinstance(assistant_msg, str):
            # Bound the search to the leading-line region (a few short lines):
            # [pj:...] always lands well within the first 200 chars whatever
            # the line order. Narrowing the window also avoids matching a
            # literal [pj:...] token deeper in the body.
            pj_m = re.search(r'\[pj:([^\]]+)\]', assistant_msg[:200])
            if pj_m:
                val = pj_m.group(1)
                if val and val not in ('(none)', '?', 'none'):
                    if os.path.isdir(os.path.join(PROGRESS_ROOT, val)):
                        state['project'] = val
                        try:
                            with open(state_path, 'w', encoding='utf-8') as f:
                                json.dump(state, f, ensure_ascii=False)
                        except OSError:
                            pass

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
        f'remaining items, or clear (header only) if the task is complete. If no '
        f'task file exists for work you did, propose creating one: state the '
        f'suggested title, status (TODO / In Progress / Done), and reason for '
        f'that status. Do NOT create the file yet — wait for user confirmation. '
        f'If the user ignores the proposal, do not create it.\n'
        f'2. For `touched` paths outside `tasks/` (source files, specs, configs): '
        f'map each to the owning task (by scope, any status) and update per (1). '
        f'Bug fixes and verification-driven tweaks ARE task progress.\n'
        f'3. Reply `[progress capture] skip — no task work` IF every `touched` '
        f'entry is unrelated to any task in this project (e.g., transient scratch '
        f'files, generated artifacts, project-notes). If a mapping is ambiguous, '
        f'skip rather than force-assign.\n'
        f'4. After completing updates, reply with exactly this format (one line per '
        f'updated task):\n'
        f'   [progress capture] done\n'
        f'   - <task-filename> ← [s:{sid8}] logged\n'
        f'   If you proposed a new task (not yet created), append:\n'
        f'   - (proposed) <suggested-title> — <status> — awaiting confirmation\n'
        f'   If no task was updated, the skip message from step 3 serves as the output.'
    )

    result = {'decision': 'block', 'reason': reason}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    sys.stdout.buffer.write(b'\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
