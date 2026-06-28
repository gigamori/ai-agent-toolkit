#!/usr/bin/env python3
"""
PostToolUse hook: record this session's file writes to a per-session
`<STATE_DIR>/<session_id>.touched` ledger (one normalized repo-relative path
per line, append-only, lock-free).

Replaces the old Stop-hook jsonl-scan + git-diff detection
(project-notes/specs/exec-binding.md §3.1/§3.2). Fires for
Write / Edit / NotebookEdit and file-touching Bash (`mv|cp|rm`, shell
redirection `>`/`>>`, and `tee`).

Provenance (exec-binding.md §3.3): capture is limited to *this session's tool
writes* — action observation, NOT filesystem result observation (git/mtime/
hash), which is the source of unrelated-task mis-stamping. PostToolUse fires
for Agent-tool subagent / fork internal tool calls with the PARENT session_id
(exec-binding.md §3.1, TBD-1 probe 2026-06-28), so subagent writes (P3) land in
the parent's `.touched` without git/jsonl.

Append is best-effort and lock-free; the Stop reader tolerates a torn trailing
line (exec-binding.md §3.2). `session_id` comes from this hook's own stdin
payload. Known parser gaps accepted as best-effort: `sed -i`, heredoc body,
`python -c open()` (exec-binding.md §3.3 / R2).
"""
import json
import os
import re
import shlex
import sys

PROGRESS_ROOT = os.path.join(os.getcwd(), '_projects')
STATE_DIR = os.path.join(PROGRESS_ROOT, '_state')

WRITE_PATH_KEYS = ('file_path', 'notebook_path')
BASH_FILE_VERBS = {'mv', 'cp', 'rm'}
BASH_TEE = 'tee'
# Split a bash command at chain operators so verb/redirect extraction in one
# segment does not bleed into another.
_BASH_CHAIN_SPLIT = re.compile(r'\s*(?:&&|\|\||;)\s*')
# Shell redirection to a file: `>` / `>>`, optionally fd-prefixed (`1>`,
# `2>>`), capturing the target token. `>&N` / `&>` (fd duplication) are skipped
# (the token after `>` must not start with `&`).
_REDIRECT_RE = re.compile(r'\d?>>?\s*(?!&)("[^"]*"|\'[^\']*\'|[^\s|&;<>()]+)')


def normalize_path(p: str, cwd: str) -> str:
    if not p:
        return p
    p = p.replace('\\', '/')
    cwd_norm = cwd.replace('\\', '/').rstrip('/')
    # Case-insensitive prefix match (Windows paths are case-insensitive).
    if p.lower().startswith(cwd_norm.lower() + '/'):
        return p[len(cwd_norm) + 1:]
    return p


def extract_bash_paths(cmd: str) -> list[str]:
    """Return file paths a bash command writes: `>`/`>>` redirection targets,
    `tee` targets, and `mv|cp|rm` non-flag args. Best-effort."""
    if not cmd or not isinstance(cmd, str):
        return []
    paths: list[str] = []
    # Redirection targets anywhere in the command.
    for m in _REDIRECT_RE.finditer(cmd):
        t = m.group(1).strip().strip('"\'')
        if t:
            paths.append(t)
    # Verb-based targets, parsed per chain segment then per pipe stage (so
    # `... | tee f` is reached and other commands' piped args are not pulled in).
    for segment in _BASH_CHAIN_SPLIT.split(cmd):
        for stage in segment.split('|'):
            stage = stage.strip()
            if not stage:
                continue
            try:
                tokens = shlex.split(stage, posix=(os.name != 'nt'))
            except ValueError:
                print(f'[touched_capture] shlex parse error: {stage[:80]}',
                      file=sys.stderr)
                continue
            if not tokens:
                continue
            verb = tokens[0]
            if verb not in BASH_FILE_VERBS and verb != BASH_TEE:
                continue
            for t in tokens[1:]:
                if t.startswith('-'):
                    continue
                if t == '>>' or t.startswith('>'):
                    break  # redirection handled separately above
                paths.append(t)
    return paths


def extract_paths(tool_input: dict) -> list[str]:
    """Collect write targets from a tool_input. The PostToolUse matcher already
    restricts firing to Write|Edit|NotebookEdit|Bash, so a present `file_path` /
    `notebook_path` is a write target and `command` is a shell command."""
    if not isinstance(tool_input, dict):
        return []
    paths: list[str] = []
    for key in WRITE_PATH_KEYS:
        v = tool_input.get(key)
        if v:
            paths.append(v)
    cmd = tool_input.get('command')
    if cmd:
        paths.extend(extract_bash_paths(cmd))
    return paths


def main() -> int:
    if not os.path.isdir(STATE_DIR):
        return 0
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    except Exception:
        return 0
    session_id = data.get('session_id', '')
    if not session_id:
        return 0
    # Only record for sessions that hold taskflow state (avoid orphan files).
    # For subagent/fork tool calls PostToolUse carries the PARENT session_id
    # (exec-binding.md §3.1), whose state file exists.
    if not os.path.exists(os.path.join(STATE_DIR, f'{session_id}.json')):
        return 0

    raw_paths = extract_paths(data.get('tool_input', {}))
    if not raw_paths:
        return 0

    cwd = os.getcwd()
    lines: list[str] = []
    seen: set[str] = set()
    for p in raw_paths:
        n = normalize_path(p, cwd)
        if n and n not in seen:
            seen.add(n)
            lines.append(n)
    if not lines:
        return 0

    touched_path = os.path.join(STATE_DIR, f'{session_id}.touched')
    # One append per invocation, built as a single newline-terminated buffer so
    # the write is a single O_APPEND syscall (best-effort atomicity) and the
    # Stop reader can drop a torn trailing line.
    blob = ''.join(f'{ln}\n' for ln in lines)
    try:
        with open(touched_path, 'a', encoding='utf-8') as f:
            f.write(blob)
    except OSError:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
